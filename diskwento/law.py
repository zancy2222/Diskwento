"""Statutory inputs: rates, eligible categories, and citation strings.

This module is the project's single source of legal truth. Two rules keep it
that way, and the rest of the codebase depends on both:

1. **The LLM never writes a citation.** It may only copy strings out of
   :data:`CITATIONS`. A 1.5B model asked to cite Philippine law will invent a
   plausible-looking RA number, and a fabricated citation in a complaint
   letter discredits the user in front of the manager. See
   :mod:`diskwento.llm` for the guard that enforces this.
2. **Anything we have not checked against the official text is flagged.**
   Citations carry ``needs_review``; ``python -m diskwento.law`` prints the
   ones to verify on an official source (Official Gazette, BIR, or the DSWD /
   NCDA issuances) before you demo. Do that -- a judge who knows the law will
   ask, and "we flagged exactly what we had not verified" is a far better
   answer than a confidently wrong section number.

Scope note: this engine audits the **arithmetic** of a discount. It does not
decide whether the cardholder's ID is valid, whether the establishment is
covered, or whether a purchase was "for the exclusive use and enjoyment" of
the cardholder. Those are judgment calls that belong to a person.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

# --------------------------------------------------------------------------
# Rates
# --------------------------------------------------------------------------

VAT_RATE = Decimal("0.12")
VAT_DIVISOR = Decimal("1.12")
DISCOUNT_RATE = Decimal("0.20")

# RA 9994's special 5% discount on basic necessities and prime commodities.
# Distinct from the 20% privilege: it is NOT accompanied by VAT exemption and
# it is capped per calendar week.
BASIC_NECESSITY_RATE = Decimal("0.05")
BASIC_NECESSITY_WEEKLY_CAP = Decimal("1300.00")


class Cardholder(Enum):
    """Who is claiming the privilege. The arithmetic is identical; the
    citations and the penalty clause differ."""

    SENIOR = "senior"
    PWD = "pwd"


class Category(Enum):
    """What was bought. Drives the treatment, and nothing else."""

    # --- 20% + VAT exemption (RA 9994 Sec. 4(a) list) ---
    MEDICINE = "medicine"
    MEDICAL_SERVICE = "medical_service"
    PROFESSIONAL_FEE = "professional_fee"
    LAB_DIAGNOSTIC = "lab_diagnostic"
    TRANSPORT_LAND = "transport_land"
    TRANSPORT_AIR_SEA = "transport_air_sea"
    HOTEL_LODGING = "hotel_lodging"
    RESTAURANT = "restaurant"
    RECREATION = "recreation"
    CINEMA_THEATER = "cinema_theater"
    FUNERAL = "funeral"

    # --- special 5%, VAT still applies, weekly cap ---
    BASIC_NECESSITY = "basic_necessity"
    PRIME_COMMODITY = "prime_commodity"

    # --- expressly excluded ---
    ALCOHOL = "alcohol"
    TOBACCO = "tobacco"

    # --- default: a plain retail sale carries no privilege ---
    OTHER_GOODS = "other_goods"


class Treatment(Enum):
    """How a line is to be computed."""

    TWENTY_PCT_VAT_EXEMPT = "20pct_vat_exempt"
    FIVE_PCT_CAPPED = "5pct_capped"
    NO_PRIVILEGE = "no_privilege"


_TWENTY_PCT = frozenset(
    {
        Category.MEDICINE,
        Category.MEDICAL_SERVICE,
        Category.PROFESSIONAL_FEE,
        Category.LAB_DIAGNOSTIC,
        Category.TRANSPORT_LAND,
        Category.TRANSPORT_AIR_SEA,
        Category.HOTEL_LODGING,
        Category.RESTAURANT,
        Category.RECREATION,
        Category.CINEMA_THEATER,
        Category.FUNERAL,
    }
)

_FIVE_PCT = frozenset({Category.BASIC_NECESSITY, Category.PRIME_COMMODITY})


def treatment_for(category: Category) -> Treatment:
    if category in _TWENTY_PCT:
        return Treatment.TWENTY_PCT_VAT_EXEMPT
    if category in _FIVE_PCT:
        return Treatment.FIVE_PCT_CAPPED
    return Treatment.NO_PRIVILEGE


# --------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """One quotable legal hook.

    ``text_tl`` is what lands in the complaint letter, so it is written to be
    read out loud to a store manager, not to a court.
    """

    key: str
    law: str
    locus: str
    text_en: str
    text_tl: str
    needs_review: bool = False

    @property
    def label(self) -> str:
        return f"{self.law}, {self.locus}"


def _c(*args: object, **kwargs: object) -> Citation:
    return Citation(*args, **kwargs)  # type: ignore[arg-type]


CITATIONS: dict[str, Citation] = {
    "SENIOR_20_VAT": _c(
        "SENIOR_20_VAT",
        "Republic Act No. 9994 (Expanded Senior Citizens Act of 2010)",
        "Sec. 4(a)",
        "Grants senior citizens a 20% discount and exemption from the 12% "
        "value-added tax on the sale of the goods and services enumerated "
        "there, for the exclusive use and enjoyment of the senior citizen.",
        "Ayon sa Republic Act No. 9994, Seksyon 4(a), ang senior citizen ay "
        "may karapatan sa 20% diskwento at exemption sa 12% VAT sa mga "
        "produkto at serbisyong nakalista rito.",
    ),
    "PWD_20_VAT": _c(
        "PWD_20_VAT",
        "Republic Act No. 10754, amending Republic Act No. 7277 "
        "(Magna Carta for Persons with Disability, as amended by RA 9442)",
        "Sec. 1 / RA 7277 Sec. 32",
        "Grants persons with disability a 20% discount and exemption from the "
        "12% value-added tax on the goods and services enumerated there.",
        "Ayon sa Republic Act No. 10754, na sumususog sa Republic Act No. "
        "7277 (Magna Carta for Persons with Disability), ang person with "
        "disability ay may karapatan sa 20% diskwento at exemption sa 12% VAT.",
    ),
    "VAT_BEFORE_DISCOUNT": _c(
        "VAT_BEFORE_DISCOUNT",
        "Bureau of Internal Revenue Revenue Regulations "
        "(RR No. 7-2010 for senior citizens; RR No. 5-2017 for PWDs)",
        "computation of the discount",
        "The 12% VAT is removed from the VAT-inclusive selling price FIRST, "
        "and the 20% discount is then computed on the resulting VAT-exempt "
        "amount. Taking 20% off the VAT-inclusive price and keeping the VAT "
        "undercharges the privilege.",
        "Ang tamang pagkuwenta po: tinatanggal muna ang 12% VAT sa presyo, "
        "pagkatapos ang 20% diskwento ay kinukuha sa halagang wala nang VAT. "
        "Hindi po tama ang basta mag-bawas ng 20% habang nananatili ang VAT.",
        needs_review=True,
    ),
    "SENIOR_BN_5": _c(
        "SENIOR_BN_5",
        "Republic Act No. 9994, as implemented by the NEDA-DTI-DA Joint "
        "Memorandum Circular on basic necessities and prime commodities",
        "special 5% discount",
        "A separate 5% special discount applies to basic necessities and "
        "prime commodities, subject to a PHP 1,300.00 ceiling per calendar "
        "week with no carry-over. This discount does NOT carry VAT exemption.",
        "May hiwalay na 5% diskwento sa basic necessities at prime "
        "commodities, hanggang ₱1,300.00 lamang kada linggo. Ang 5% na "
        "ito ay walang kasamang VAT exemption.",
        needs_review=True,
    ),
    "PWD_BN_5": _c(
        "PWD_BN_5",
        "Republic Act No. 10754 and its implementing rules on basic "
        "necessities and prime commodities",
        "special 5% discount",
        "A separate 5% special discount applies to basic necessities and "
        "prime commodities, subject to a weekly ceiling. This discount does "
        "NOT carry VAT exemption.",
        "May hiwalay na 5% diskwento sa basic necessities at prime "
        "commodities, may takdang limitasyon kada linggo. Walang kasamang "
        "VAT exemption ang 5% na ito.",
        needs_review=True,
    ),
    "EXCLUDED_ITEMS": _c(
        "EXCLUDED_ITEMS",
        "Republic Act No. 9994 and its implementing rules",
        "scope of Sec. 4(a)",
        "The 20% privilege covers the enumerated goods and services. Alcoholic "
        "beverages, tobacco, and ordinary retail merchandise outside that "
        "list are not covered.",
        "Ang 20% diskwento ay para lamang sa mga produkto at serbisyong "
        "nakalista sa batas. Hindi kasama ang alak, sigarilyo, at mga "
        "ordinaryong paninda sa labas ng listahang iyon.",
        needs_review=True,
    ),
    "SHARED_BILL": _c(
        "SHARED_BILL",
        "Republic Act No. 9994 and its implementing rules",
        "exclusive use and enjoyment",
        "The privilege covers only what the cardholder personally consumed. "
        "On a bill shared by a group, it applies to the cardholder's share, "
        "not the whole bill.",
        "Ang diskwento po ay para lamang sa bahaging kinonsumo ng may hawak "
        "ng ID. Sa bill na hati-hati sa grupo, sa share lamang niya ito "
        "ipinapatong at hindi sa buong bill.",
        needs_review=True,
    ),
    "NO_DOUBLE_DISCOUNT": _c(
        "NO_DOUBLE_DISCOUNT",
        "Republic Act No. 9994 and its implementing rules",
        "no double discount",
        "The 20% privilege may not be stacked on top of an existing "
        "promotional discount. Where both could apply, the cardholder avails "
        "of whichever is higher.",
        "Hindi po maaaring isabay ang 20% diskwento sa isang promotional "
        "discount. Kung pareho pong puwede, ang mas mataas po ang dapat "
        "gamitin.",
        needs_review=True,
    ),
    "NON_VAT_SELLER": _c(
        "NON_VAT_SELLER",
        "Bureau of Internal Revenue Revenue Regulations "
        "(RR No. 7-2010; RR No. 5-2017)",
        "non-VAT registered sellers",
        "Where the seller is not VAT-registered, there is no VAT to strip out "
        "and the 20% discount is computed directly on the selling price.",
        "Kung ang tindahan po ay hindi VAT-registered, wala pong VAT na "
        "tatanggalin at ang 20% diskwento ay direktang kinukuha sa presyo.",
        needs_review=True,
    ),
    "PENALTY_SENIOR": _c(
        "PENALTY_SENIOR",
        "Republic Act No. 9994",
        "Sec. 10",
        "Refusal to grant the statutory discount is punishable. For the first "
        "offense the law provides a fine and imprisonment.",
        "May kaukulang parusa po sa batas ang pagtanggi na ibigay ang "
        "diskwentong nakasaad sa RA 9994.",
        needs_review=True,
    ),
    "PENALTY_PWD": _c(
        "PENALTY_PWD",
        "Republic Act No. 7277, as amended (RA 9442, RA 10754)",
        "Sec. 46",
        "Refusal to grant the statutory discount is punishable. For the first "
        "offense the law provides a fine and/or imprisonment.",
        "May kaukulang parusa po sa batas ang pagtanggi na ibigay ang "
        "diskwentong nakasaad sa Magna Carta for Persons with Disability.",
        needs_review=True,
    ),
}


def privilege_citation(cardholder: Cardholder) -> Citation:
    return CITATIONS[
        "SENIOR_20_VAT" if cardholder is Cardholder.SENIOR else "PWD_20_VAT"
    ]


def basic_necessity_citation(cardholder: Cardholder) -> Citation:
    return CITATIONS[
        "SENIOR_BN_5" if cardholder is Cardholder.SENIOR else "PWD_BN_5"
    ]


def penalty_citation(cardholder: Cardholder) -> Citation:
    return CITATIONS[
        "PENALTY_SENIOR" if cardholder is Cardholder.SENIOR else "PENALTY_PWD"
    ]


def unverified() -> list[Citation]:
    """Citations still to be checked against an official source."""
    return [c for c in CITATIONS.values() if c.needs_review]


def _audit() -> None:
    pending = unverified()
    total = len(CITATIONS)
    print(f"{total - len(pending)}/{total} citations marked verified.\n")
    if not pending:
        print("All citations verified.")
        return
    print("VERIFY THESE AGAINST THE OFFICIAL TEXT BEFORE DEMOING:\n")
    for c in pending:
        print(f"  [{c.key}]")
        print(f"    {c.law}")
        print(f"    locus: {c.locus}\n")
    print(
        "Sources: Official Gazette (officialgazette.gov.ph), BIR issuances\n"
        "(bir.gov.ph), and the DSWD / NCDA implementing rules.\n"
        "Once a string matches the official text, set needs_review=False."
    )


if __name__ == "__main__":
    _audit()
