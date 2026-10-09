"""The deterministic math engine.

This module decides, by arithmetic alone, whether a receipt shortchanged a
senior citizen or a person with disability. No model runs here. Given the same
receipt it returns the same verdict forever, it is unit-testable, and it is the
only thing in the project allowed to produce a number.

The computation the law prescribes, on a VAT-inclusive selling price:

    selling price (VAT-inclusive)
      / 1.12   -> VAT-exempt sale          (the 12% VAT comes off FIRST)
      * 0.20   -> the statutory discount   (computed on the VAT-exempt amount)
      -> amount due = VAT-exempt sale - discount = VAT-exempt sale * 0.80

So PHP 112.00 of covered goods must be billed at PHP 80.00, not PHP 89.60.
The effective reduction off the shelf price is 28.57%, not 20%, and the gap
between those two numbers is this entire application's reason to exist: a
cashier who takes "20% off" at face value overcharges by 12% of the discounted
base every single time.

Beyond recomputing the total, :func:`audit` *fingerprints* the merchant's
arithmetic: it replays the handful of ways POS systems get this wrong and
reports which one reproduces the printed total. Naming the mistake ("the VAT
was never removed") is what makes a complaint letter persuasive, and it keeps
the language model's job purely linguistic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Iterator

from .law import (
    BASIC_NECESSITY_RATE,
    BASIC_NECESSITY_WEEKLY_CAP,
    CITATIONS,
    Cardholder,
    Category,
    Citation,
    DISCOUNT_RATE,
    Treatment,
    VAT_DIVISOR,
    VAT_RATE,
    basic_necessity_citation,
    penalty_citation,
    privilege_citation,
    treatment_for,
)
from .money import ZERO, fmt, peso, pct, q

# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LineItem:
    """One line as printed on the receipt.

    ``gross`` is the amount the receipt shows for this line -- VAT-inclusive
    when the seller is VAT-registered, which is the normal case in Philippine
    retail.

    ``diners_sharing`` handles the shared restaurant bill: a PHP 2,000.00
    table split by four people, one of whom is a senior, carries the privilege
    on PHP 500.00 only. Leave it at 1 for a purchase that is entirely the
    cardholder's.

    ``promo_discount`` is a promotional discount already granted on this line.
    The statutory privilege cannot be stacked on top of one; the engine takes
    whichever is better for the cardholder and says so.
    """

    description: str
    gross: Decimal
    category: Category = Category.OTHER_GOODS
    diners_sharing: int = 1
    promo_discount: Decimal = ZERO

    def __post_init__(self) -> None:
        object.__setattr__(self, "gross", peso(self.gross))
        object.__setattr__(self, "promo_discount", peso(self.promo_discount))
        if self.diners_sharing < 1:
            raise ValueError("diners_sharing must be at least 1")
        if self.gross < ZERO:
            raise ValueError(f"line {self.description!r} has a negative amount")


@dataclass(frozen=True)
class Receipt:
    """A receipt as read off the paper, plus who was claiming the privilege.

    The ``printed_*`` fields are what the receipt actually says. Any of them
    may be ``None`` when OCR could not read it; the engine then reports what
    the total *should* be and marks the verdict unverifiable rather than
    guessing.
    """

    cardholder: Cardholder
    lines: tuple[LineItem, ...]
    seller_vat_registered: bool = True
    service_charge: Decimal = ZERO
    printed_total: Decimal | None = None
    printed_discount: Decimal | None = None
    printed_vat: Decimal | None = None
    # Basic necessities already bought this calendar week, against the
    # PHP 1,300.00 ceiling. The user supplies this; a receipt cannot know it.
    basic_necessity_spent_this_week: Decimal = ZERO
    merchant: str = ""
    receipt_no: str = ""
    date: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "lines", tuple(self.lines))
        object.__setattr__(self, "service_charge", peso(self.service_charge))
        object.__setattr__(
            self,
            "basic_necessity_spent_this_week",
            peso(self.basic_necessity_spent_this_week),
        )
        for name in ("printed_total", "printed_discount", "printed_vat"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, peso(value))
        if not self.lines:
            raise ValueError("a receipt needs at least one line item")


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------


class Status(str):
    """Verdict markers. Plain strings so they serialize without ceremony."""


CORRECT = "CORRECT"
SHORTCHANGED = "SHORTCHANGED"
OVER_DISCOUNTED = "OVER_DISCOUNTED"
CANNOT_VERIFY = "CANNOT_VERIFY"


@dataclass
class LineResult:
    description: str
    category: Category
    treatment: Treatment
    gross: Decimal
    covered_gross: Decimal
    vat_exempt_base: Decimal
    vat_removed: Decimal
    discount: Decimal
    amount_due: Decimal
    notes: list[str] = field(default_factory=list)


@dataclass
class Diagnosis:
    """Which known miscomputation reproduces the printed total."""

    pattern: str
    headline_en: str
    headline_tl: str
    detail_en: str
    detail_tl: str
    citations: tuple[Citation, ...] = ()
    matched: bool = True


@dataclass
class Audit:
    receipt: Receipt
    lines: list[LineResult]
    expected_vat_exempt_base: Decimal
    expected_vat_removed: Decimal
    expected_discount: Decimal
    expected_total: Decimal
    covered_gross: Decimal
    status: str
    overcharge: Decimal
    tolerance: Decimal
    diagnosis: Diagnosis | None
    steps: list[str]
    warnings: list[str] = field(default_factory=list)

    @property
    def shortchanged(self) -> bool:
        return self.status == SHORTCHANGED

    @property
    def citations(self) -> tuple[Citation, ...]:
        """Every citation this audit relies on, de-duplicated, in order."""
        out: list[Citation] = [privilege_citation(self.receipt.cardholder)]
        if self.diagnosis:
            out.extend(self.diagnosis.citations)
        if self.shortchanged:
            out.append(penalty_citation(self.receipt.cardholder))
        seen: set[str] = set()
        unique: list[Citation] = []
        for c in out:
            if c.key not in seen:
                seen.add(c.key)
                unique.append(c)
        return tuple(unique)


# --------------------------------------------------------------------------
# Per-line computation
# --------------------------------------------------------------------------


def _split_shared(line: LineItem) -> list[LineItem]:
    """Split a shared bill into the cardholder's covered share and the rest."""
    if line.diners_sharing == 1:
        return [line]
    share = q(line.gross / Decimal(line.diners_sharing))
    remainder = line.gross - share
    covered = replace(
        line,
        description=f"{line.description} (share ng may ID, 1/{line.diners_sharing})",
        gross=share,
        diners_sharing=1,
        promo_discount=ZERO,
    )
    others = LineItem(
        description=(
            f"{line.description} (share ng kasama, "
            f"{line.diners_sharing - 1}/{line.diners_sharing})"
        ),
        gross=remainder,
        category=Category.OTHER_GOODS,
    )
    return [covered, others]


def _compute_line(
    line: LineItem,
    *,
    vat_registered: bool,
    cardholder: Cardholder,
    bn_cap_left: Decimal,
) -> tuple[LineResult, Decimal]:
    """Compute one line. Returns the result and the basic-necessity cap used."""
    treatment = treatment_for(line.category)
    notes: list[str] = []

    if treatment is Treatment.TWENTY_PCT_VAT_EXEMPT:
        if vat_registered:
            base = q(line.gross / VAT_DIVISOR)
            vat_removed = line.gross - base
        else:
            base = line.gross
            vat_removed = ZERO
            notes.append(
                "Hindi VAT-registered ang tindahan, kaya walang VAT na "
                "tatanggalin; direkta ang 20% sa presyo."
            )
        discount = q(base * DISCOUNT_RATE)
        amount_due = base - discount

        # No double discount: take whichever is better for the cardholder.
        if line.promo_discount > ZERO:
            promo_due = line.gross - line.promo_discount
            if promo_due < amount_due:
                notes.append(
                    f"Mas mataas ang promo na {fmt(line.promo_discount)} kaysa "
                    f"sa statutory na {fmt(line.gross - amount_due)}; ang promo "
                    "po ang ginamit dahil hindi maaaring isabay ang dalawa."
                )
                return (
                    LineResult(
                        description=line.description,
                        category=line.category,
                        treatment=Treatment.NO_PRIVILEGE,
                        gross=line.gross,
                        covered_gross=ZERO,
                        vat_exempt_base=ZERO,
                        vat_removed=ZERO,
                        discount=line.promo_discount,
                        amount_due=promo_due,
                        notes=notes,
                    ),
                    ZERO,
                )
            notes.append(
                f"May promo na {fmt(line.promo_discount)} pero mas mataas ang "
                "statutory discount, kaya ito ang ginamit."
            )

        return (
            LineResult(
                description=line.description,
                category=line.category,
                treatment=treatment,
                gross=line.gross,
                covered_gross=line.gross,
                vat_exempt_base=base,
                vat_removed=vat_removed,
                discount=discount,
                amount_due=amount_due,
                notes=notes,
            ),
            ZERO,
        )

    if treatment is Treatment.FIVE_PCT_CAPPED:
        # The 5% runs on the selling price as printed; there is no VAT
        # exemption attached to it, and it stops at the weekly ceiling.
        eligible = min(line.gross, bn_cap_left) if bn_cap_left > ZERO else ZERO
        discount = q(eligible * BASIC_NECESSITY_RATE)
        if eligible < line.gross:
            notes.append(
                f"Umabot na sa ₱{BASIC_NECESSITY_WEEKLY_CAP:,.2f} kada "
                f"linggo na limitasyon; {fmt(eligible)} lamang ang may 5% "
                "diskwento sa linyang ito."
            )
        notes.append("Walang VAT exemption ang 5% sa basic necessities.")
        return (
            LineResult(
                description=line.description,
                category=line.category,
                treatment=treatment,
                gross=line.gross,
                covered_gross=eligible,
                vat_exempt_base=ZERO,
                vat_removed=ZERO,
                discount=discount,
                amount_due=line.gross - discount,
                notes=notes,
            ),
            eligible,
        )

    # No privilege. Honour any promo the merchant gave.
    if line.category in (Category.ALCOHOL, Category.TOBACCO):
        notes.append("Hindi kasama sa 20% diskwento ang alak at sigarilyo.")
    elif line.category is Category.OTHER_GOODS:
        notes.append(
            "Ordinaryong paninda; wala pong 20% diskwento maliban kung ito ay "
            "basic necessity o prime commodity."
        )
    return (
        LineResult(
            description=line.description,
            category=line.category,
            treatment=Treatment.NO_PRIVILEGE,
            gross=line.gross,
            covered_gross=ZERO,
            vat_exempt_base=ZERO,
            vat_removed=ZERO,
            discount=line.promo_discount,
            amount_due=line.gross - line.promo_discount,
            notes=notes,
        ),
        ZERO,
    )


# --------------------------------------------------------------------------
# Fingerprinting the merchant's arithmetic
# --------------------------------------------------------------------------

_WRONG_RATES = (
    Decimal("0.05"),
    Decimal("0.10"),
    Decimal("0.12"),
    Decimal("0.15"),
    Decimal("0.25"),
)


def _candidates(
    covered_gross: Decimal, base: Decimal, vat_registered: bool
) -> Iterator[tuple[str, Decimal]]:
    """Replay the ways a POS gets this wrong, as amounts due on the covered
    portion. ``CORRECT`` is included so a correct receipt fingerprints too."""
    yield "CORRECT", q(base * (Decimal(1) - DISCOUNT_RATE))

    if vat_registered:
        # The big one: 20% taken off the VAT-inclusive price with the VAT
        # left in. Numerically identical to re-adding VAT after a correct
        # discount, so the two merchant stories share one pattern.
        yield "VAT_NOT_EXEMPTED", q(covered_gross * (Decimal(1) - DISCOUNT_RATE))
        yield "VAT_EXEMPT_ONLY", base
        yield "VAT_DEDUCTED_TWICE", q(
            q(base / VAT_DIVISOR) * (Decimal(1) - DISCOUNT_RATE)
        )

    yield "NO_PRIVILEGE_APPLIED", covered_gross

    for rate in _WRONG_RATES:
        yield f"WRONG_RATE_ON_BASE:{rate}", q(base * (Decimal(1) - rate))
        if vat_registered:
            yield f"WRONG_RATE_ON_GROSS:{rate}", q(
                covered_gross * (Decimal(1) - rate)
            )


def _describe(
    pattern: str,
    *,
    cardholder: Cardholder,
    covered_gross: Decimal,
    base: Decimal,
    correct_due: Decimal,
    observed_due: Decimal,
) -> Diagnosis:
    gap = observed_due - correct_due
    priv = privilege_citation(cardholder)
    vat_rule = CITATIONS["VAT_BEFORE_DISCOUNT"]

    if pattern == "CORRECT":
        return Diagnosis(
            pattern="CORRECT",
            headline_en="Discount and VAT exemption correctly applied.",
            headline_tl="Tama po ang diskwento at VAT exemption.",
            detail_en=(
                f"The VAT was removed from {fmt(covered_gross)} to give a "
                f"VAT-exempt sale of {fmt(base)}, and the 20% discount was "
                f"computed on that amount."
            ),
            detail_tl=(
                f"Tinanggal po ang VAT sa {fmt(covered_gross)} kaya naging "
                f"{fmt(base)} ang VAT-exempt sale, at doon kinuha ang 20% "
                "diskwento."
            ),
            citations=(priv,),
        )

    if pattern == "VAT_NOT_EXEMPTED":
        return Diagnosis(
            pattern=pattern,
            headline_en="The 12% VAT was never removed.",
            headline_tl="Hindi tinanggal ang 12% VAT.",
            detail_en=(
                f"The 20% was taken off the VAT-inclusive price "
                f"{fmt(covered_gross)} while the 12% VAT stayed on the bill. "
                f"The VAT should have been removed first, giving a VAT-exempt "
                f"sale of {fmt(base)} and an amount due of {fmt(correct_due)} "
                f"instead of {fmt(observed_due)} -- a difference of {fmt(gap)}."
            ),
            detail_tl=(
                f"Kinuha po ang 20% sa presyong may VAT pa ({fmt(covered_gross)}) "
                f"at nanatili ang 12% VAT sa bill. Dapat po ay tinanggal muna "
                f"ang VAT para maging {fmt(base)} ang VAT-exempt sale, at "
                f"{fmt(correct_due)} na lamang ang babayaran kaysa "
                f"{fmt(observed_due)} -- sobra po ng {fmt(gap)}."
            ),
            citations=(priv, vat_rule),
        )

    if pattern == "VAT_EXEMPT_ONLY":
        return Diagnosis(
            pattern=pattern,
            headline_en="VAT was removed but the 20% discount was not given.",
            headline_tl="Tinanggal ang VAT pero walang 20% diskwento.",
            detail_en=(
                f"The bill came to the VAT-exempt sale of {fmt(base)}, so the "
                f"VAT exemption was honoured but the 20% discount of "
                f"{fmt(base - correct_due)} was never deducted."
            ),
            detail_tl=(
                f"Ang binayaran po ay {fmt(base)}, ang VAT-exempt sale, kaya "
                f"naibigay ang VAT exemption pero hindi po naibawas ang 20% "
                f"diskwento na {fmt(base - correct_due)}."
            ),
            citations=(priv,),
        )

    if pattern == "NO_PRIVILEGE_APPLIED":
        return Diagnosis(
            pattern=pattern,
            headline_en="Neither the discount nor the VAT exemption was applied.",
            headline_tl="Walang diskwento at walang VAT exemption.",
            detail_en=(
                f"The full VAT-inclusive price {fmt(covered_gross)} was "
                f"charged. The amount due should have been {fmt(correct_due)} "
                f"-- an overcharge of {fmt(gap)}."
            ),
            detail_tl=(
                f"Buong presyo pong {fmt(covered_gross)} ang ibinayad. Dapat po "
                f"ay {fmt(correct_due)} lamang -- sobra po ng {fmt(gap)}."
            ),
            citations=(priv, vat_rule),
        )

    if pattern == "VAT_DEDUCTED_TWICE":
        return Diagnosis(
            pattern=pattern,
            headline_en="The VAT appears to have been removed twice.",
            headline_tl="Mukhang dalawang beses tinanggal ang VAT.",
            detail_en=(
                f"The amount due {fmt(observed_due)} is lower than the "
                f"{fmt(correct_due)} the law requires, consistent with the "
                f"VAT being divided out twice. This favours the customer; no "
                f"complaint is warranted."
            ),
            detail_tl=(
                f"Mas mababa po ang binayaran ({fmt(observed_due)}) kaysa sa "
                f"{fmt(correct_due)} na nasa batas. Pabor po ito sa customer, "
                "kaya wala pong dapat ireklamo."
            ),
            citations=(priv,),
        )

    if pattern == "BASIC_NECESSITY_DISCOUNT_MISSING":
        return Diagnosis(
            pattern=pattern,
            headline_en="The 5% basic necessities discount was not given.",
            headline_tl="Hindi naibigay ang 5% sa basic necessities.",
            detail_en=(
                f"The covered goods and services were computed correctly, but "
                f"the separate 5% discount on basic necessities and prime "
                f"commodities was never deducted. The gap is exactly "
                f"{fmt(gap)}."
            ),
            detail_tl=(
                f"Tama po ang kuwenta sa mga items na saklaw ng 20%, pero "
                f"hindi po naibawas ang hiwalay na 5% diskwento sa basic "
                f"necessities at prime commodities. Ang kulang po ay "
                f"{fmt(gap)}."
            ),
            citations=(priv, basic_necessity_citation(cardholder)),
        )

    if pattern.startswith("WRONG_RATE_ON_BASE:"):
        rate = Decimal(pattern.split(":", 1)[1])
        return Diagnosis(
            pattern=pattern,
            headline_en=f"A {pct(rate)} discount was given instead of 20%.",
            headline_tl=f"{pct(rate)} ang naibigay na diskwento, hindi 20%.",
            detail_en=(
                f"The VAT was correctly removed to give {fmt(base)}, but the "
                f"discount was computed at {pct(rate)} rather than the "
                f"statutory 20%. The amount due should be {fmt(correct_due)}, "
                f"not {fmt(observed_due)} -- a difference of {fmt(gap)}."
            ),
            detail_tl=(
                f"Tama po ang pagtanggal ng VAT ({fmt(base)}) pero {pct(rate)} "
                f"lamang ang diskwentong kinuha, hindi ang 20% na nasa batas. "
                f"Dapat po ay {fmt(correct_due)} ang babayaran at hindi "
                f"{fmt(observed_due)} -- sobra po ng {fmt(gap)}."
            ),
            citations=(priv,),
        )

    if pattern.startswith("WRONG_RATE_ON_GROSS:"):
        rate = Decimal(pattern.split(":", 1)[1])
        return Diagnosis(
            pattern=pattern,
            headline_en=(
                f"A {pct(rate)} discount was taken off the VAT-inclusive price "
                "and the VAT was never removed."
            ),
            headline_tl=(
                f"{pct(rate)} lamang ang kinuha sa presyong may VAT, at hindi "
                "tinanggal ang VAT."
            ),
            detail_en=(
                f"Two errors at once: the rate was {pct(rate)} instead of 20%, "
                f"and it was applied to the VAT-inclusive price "
                f"{fmt(covered_gross)} rather than to the VAT-exempt sale "
                f"{fmt(base)}. The amount due should be {fmt(correct_due)}, "
                f"not {fmt(observed_due)} -- a difference of {fmt(gap)}."
            ),
            detail_tl=(
                f"Dalawa po ang mali: {pct(rate)} ang ginamit kaysa 20%, at "
                f"kinuha ito sa presyong may VAT ({fmt(covered_gross)}) at "
                f"hindi sa VAT-exempt sale ({fmt(base)}). Dapat po ay "
                f"{fmt(correct_due)} at hindi {fmt(observed_due)} -- sobra po "
                f"ng {fmt(gap)}."
            ),
            citations=(priv, vat_rule),
        )

    # No known pattern reproduced the printed total. Say exactly that; do not
    # invent a story for the model to dress up.
    return Diagnosis(
        pattern="UNRECOGNIZED",
        headline_en="The total does not match any standard computation.",
        headline_tl="Hindi tugma ang total sa alinmang karaniwang kuwenta.",
        detail_en=(
            f"The amount due should be {fmt(correct_due)} but the receipt "
            f"shows {fmt(observed_due)}, a difference of {fmt(gap)}. The "
            f"engine could not identify which step went wrong, so the figures "
            f"are worth checking with the cashier before anything else."
        ),
        detail_tl=(
            f"Dapat po ay {fmt(correct_due)} ang babayaran pero {fmt(observed_due)} "
            f"ang nasa resibo, kaya may {fmt(gap)} na pagkakaiba. Hindi po "
            "matukoy kung saang hakbang nagkamali, kaya mainam pong ipatingin "
            "muna ang kuwenta sa cashier."
        ),
        citations=(privilege_citation(cardholder),),
        matched=False,
    )


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------


def audit(receipt: Receipt) -> Audit:
    """Recompute a receipt against RA 9994 / RA 10754 and name any shortfall."""
    cardholder = receipt.cardholder
    vat_registered = receipt.seller_vat_registered

    expanded: list[LineItem] = []
    shared_bill = False
    for line in receipt.lines:
        parts = _split_shared(line)
        shared_bill = shared_bill or len(parts) > 1
        expanded.extend(parts)

    cap_left = max(
        ZERO, BASIC_NECESSITY_WEEKLY_CAP - receipt.basic_necessity_spent_this_week
    )
    results: list[LineResult] = []
    for line in expanded:
        result, cap_used = _compute_line(
            line,
            vat_registered=vat_registered,
            cardholder=cardholder,
            bn_cap_left=cap_left,
        )
        cap_left -= cap_used
        results.append(result)

    twenty = [r for r in results if r.treatment is Treatment.TWENTY_PCT_VAT_EXEMPT]
    five = [r for r in results if r.treatment is Treatment.FIVE_PCT_CAPPED]

    expected_base = sum((r.vat_exempt_base for r in twenty), ZERO)
    expected_vat_removed = sum((r.vat_removed for r in twenty), ZERO)
    expected_discount = sum((r.discount for r in results), ZERO)
    expected_total = (
        sum((r.amount_due for r in results), ZERO) + receipt.service_charge
    )
    covered_gross = sum((r.covered_gross for r in twenty), ZERO)
    correct_due = sum((r.amount_due for r in twenty), ZERO)

    warnings: list[str] = []
    if shared_bill:
        warnings.append(
            "Hinati ang bill: ang diskwento ay sa share lamang ng may hawak ng ID."
        )
    if receipt.service_charge > ZERO:
        warnings.append(
            f"May service charge na {fmt(receipt.service_charge)}. Hindi po ito "
            "kasama sa base ng diskwento; ipatanong po kung tama itong ipataw."
        )
    if not vat_registered:
        warnings.append(
            "Itinuring na hindi VAT-registered ang tindahan, kaya walang VAT na "
            "tinanggal."
        )
    if five and cap_left <= ZERO:
        warnings.append(
            f"Nagamit na ang buong ₱{BASIC_NECESSITY_WEEKLY_CAP:,.2f} na "
            "limitasyon kada linggo para sa basic necessities."
        )

    steps = _build_steps(
        covered_gross=covered_gross,
        base=expected_base,
        vat_removed=expected_vat_removed,
        discount=sum((r.discount for r in twenty), ZERO),
        due=correct_due,
        vat_registered=vat_registered,
    )
    for r in five:
        if r.discount <= ZERO:
            continue  # cap already exhausted; the warning covers it
        steps.append(
            f"Basic necessities: {fmt(r.covered_gross)} × 5% = "
            f"{fmt(r.discount)} diskwento (walang VAT exemption)"
        )

    # When the receipt is more than just the covered items -- a shared bill, a
    # mixed basket, a service charge -- show how the covered subtotal becomes
    # the whole-receipt total. Without this the letter quotes a share figure
    # next to a bill figure and reads like a contradiction to a manager.
    rest = expected_total - correct_due
    if rest != ZERO:
        steps.append(f"Iba pang items na wala sa 20% diskwento: {fmt(rest)}")
        steps.append(f"Kabuuang dapat bayaran: {fmt(expected_total)}")

    tolerance = max(Decimal("0.05"), Decimal("0.01") * len(results))

    # Verdict.
    if receipt.printed_total is None:
        status = CANNOT_VERIFY
        overcharge = ZERO
        diagnosis = None
        warnings.append(
            "Hindi nabasa ang total sa resibo, kaya ang ipinapakita ay ang "
            "dapat na halaga at hindi isang hatol."
        )
    else:
        overcharge = receipt.printed_total - expected_total
        if abs(overcharge) <= tolerance:
            status = CORRECT
        elif overcharge > ZERO:
            status = SHORTCHANGED
        else:
            status = OVER_DISCOUNTED

        # First, the one error that lives outside the 20% portion: a grocery
        # that computes the covered lines correctly and simply never applies
        # the separate 5%. If the gap is exactly that discount, the covered
        # portion must have been right, so this is unambiguous.
        bn_discount = sum((r.discount for r in five), ZERO)
        if five and bn_discount > ZERO and abs(overcharge - bn_discount) <= tolerance:
            diagnosis = _describe(
                "BASIC_NECESSITY_DISCOUNT_MISSING",
                cardholder=cardholder,
                covered_gross=covered_gross,
                base=expected_base,
                correct_due=correct_due,
                observed_due=correct_due + overcharge,
            )
        else:
            # Otherwise fingerprint the covered portion: hold everything else
            # at its expected value and see which computation reproduces what
            # was actually paid.
            fixed = expected_total - correct_due
            observed_due = receipt.printed_total - fixed
            best: tuple[Decimal, str] | None = None
            for key, amount in _candidates(
                covered_gross, expected_base, vat_registered
            ):
                delta = abs(observed_due - amount)
                if best is None or delta < best[0]:
                    best = (delta, key)
            pattern = best[1] if best and best[0] <= tolerance else "UNRECOGNIZED"
            diagnosis = _describe(
                pattern,
                cardholder=cardholder,
                covered_gross=covered_gross,
                base=expected_base,
                correct_due=correct_due,
                observed_due=observed_due,
            )

    if (
        receipt.printed_discount is not None
        and abs(receipt.printed_discount - expected_discount) > tolerance
    ):
        warnings.append(
            f"Ang diskwentong nakalimbag ({fmt(receipt.printed_discount)}) ay "
            f"hindi tugma sa dapat na {fmt(expected_discount)}."
        )
    if (
        receipt.printed_vat is not None
        and twenty
        and receipt.printed_vat > tolerance
        and vat_registered
    ):
        warnings.append(
            f"May nakalimbag na VAT na {fmt(receipt.printed_vat)} kahit may "
            "VAT-exempt na items sa resibo."
        )

    return Audit(
        receipt=receipt,
        lines=results,
        expected_vat_exempt_base=expected_base,
        expected_vat_removed=expected_vat_removed,
        expected_discount=expected_discount,
        expected_total=expected_total,
        covered_gross=covered_gross,
        status=status,
        overcharge=overcharge,
        tolerance=tolerance,
        diagnosis=diagnosis,
        steps=steps,
        warnings=warnings,
    )


def _build_steps(
    *,
    covered_gross: Decimal,
    base: Decimal,
    vat_removed: Decimal,
    discount: Decimal,
    due: Decimal,
    vat_registered: bool,
) -> list[str]:
    """The arithmetic trail, ready to print on screen or in a letter."""
    if covered_gross <= ZERO:
        return ["Walang item sa resibo na saklaw ng 20% diskwento."]
    if not vat_registered:
        return [
            f"Presyo ng mga covered na item: {fmt(covered_gross)}",
            "Hindi VAT-registered ang tindahan, walang VAT na tatanggalin",
            f"Bawas 20% diskwento: {fmt(discount)}",
            f"Dapat bayaran: {fmt(due)}",
        ]
    return [
        f"Presyo ng mga covered na item (may VAT): {fmt(covered_gross)}",
        f"Hatiin sa 1.12 para tanggalin ang 12% VAT: {fmt(base)}",
        f"VAT na tinanggal: {fmt(vat_removed)}",
        f"Bawas 20% diskwento sa {fmt(base)}: {fmt(discount)}",
        f"Dapat bayaran: {fmt(due)}",
    ]


# --------------------------------------------------------------------------
# The LLM contract
# --------------------------------------------------------------------------


def to_brief(result: Audit) -> dict:
    """Pack an audit into the compact, pre-formatted brief the model reads.

    Every number here is already a finished string with its peso sign and
    centavos, because a 1.5B model asked to format currency will drop a
    centavo or move a comma. The model's only job is to write sentences
    around strings it copies verbatim.
    """
    r = result.receipt
    d = result.diagnosis
    return {
        "status": result.status,
        "cardholder": "senior citizen" if r.cardholder is Cardholder.SENIOR else "PWD",
        "merchant": r.merchant or "ang establisimyento",
        "date": r.date,
        "receipt_no": r.receipt_no,
        "amounts": {
            "covered_price_with_vat": fmt(result.covered_gross),
            "vat_exempt_sale": fmt(result.expected_vat_exempt_base),
            "vat_removed": fmt(result.expected_vat_removed),
            "correct_discount": fmt(result.expected_discount),
            "correct_total": fmt(result.expected_total),
            "printed_total": fmt(r.printed_total) if r.printed_total is not None else None,
            "overcharge": fmt(abs(result.overcharge)),
        },
        "computation_steps": result.steps,
        "finding_tl": d.headline_tl if d else "",
        "explanation_tl": d.detail_tl if d else "",
        "pattern": d.pattern if d else "NOT_APPLICABLE",
        "warnings_tl": result.warnings,
        # The ONLY legal strings the model may use. See diskwento.llm for the
        # guard that rejects output containing anything else.
        "citations": [
            {"law": c.law, "locus": c.locus, "text_tl": c.text_tl}
            for c in result.citations
        ],
    }
