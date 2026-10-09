"""OCR text to a structured :class:`~diskwento.engine.Receipt`.

This is the least reliable stage in the pipeline and the design says so out
loud. Thermal paper plus a phone camera produces misread digits, and item
descriptions on Philippine receipts are abbreviated past the point of
machine comprehension ("MM CHKN JOY W/ RC"). So the parser:

* repairs the digit confusions OCR reliably makes, but only inside numbers;
* infers the establishment type from the header, which is what resolves the
  genuinely ambiguous items -- "RICE" is a restaurant item at a carinderia and
  a basic necessity at a grocery, and the discount differs by 15 percentage
  points between those two readings;
* defaults unknown items to the treatment that does NOT grant a discount, so a
  parsing failure under-claims rather than sending someone to a manager with a
  wrong number;
* returns everything it was unsure about in ``needs_confirmation``, for the UI
  to put in front of the user before any letter gets written.

The last two points are the important ones. A false "you were shortchanged" is
far more damaging to this app's user than a missed detection: it sends a senior
citizen to argue with a manager who turns out to be right.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from .engine import LineItem, Receipt
from .law import Cardholder, Category
from .money import ZERO, peso

# --------------------------------------------------------------------------
# Field extraction
# --------------------------------------------------------------------------

# Trailing money. The character class admits the letters OCR substitutes for
# digits, because the repair in _to_amount runs *after* this match -- a strict
# "[\d,]+\.\d{2}" never sees "6O.OO" at all and the line is dropped silently.
_MONEY_AT_END = re.compile(
    r"(-?\(?[\dOoDlI|SsBZ,.]*[\dOoDlI|SsBZ][.,][\dOoDlI|SsBZ]{2}\)?)\s*$"
)
# The decimal point is whatever separator sits before the last two digits;
# everything before it is a thousands separator. OCR confuses "." and ",".
_DECIMAL_TAIL = re.compile(r"[.,](\d{2})$")

# OCR on dot-matrix receipts confuses these constantly. Applied to numbers only
# -- running it over item names would mangle them.
_DIGIT_FIXES = str.maketrans({"O": "0", "o": "0", "D": "0", "l": "1", "I": "1",
                              "|": "1", "S": "5", "s": "5", "B": "8", "Z": "2"})

_LABELS: dict[str, tuple[str, ...]] = {
    "vat_exempt_sale": ("vat exempt sale", "vat-exempt sale", "vatexempt sale",
                        "vat exempt", "exempt sale"),
    "vatable_sale": ("vatable sale", "vat sale", "vatable"),
    "vat": ("vat 12%", "12% vat", "vat amount", "output vat", "vat payable"),
    "discount": ("sc disc", "pwd disc", "sc/pwd disc", "senior disc",
                 "senior citizen disc", "less disc", "less: disc", "discount",
                 "sc/pwd", "disc amount"),
    "service_charge": ("service charge", "svc chg", "svc charge",
                       "service chrg"),
    "total": ("amount due", "total due", "total amount", "net amount",
              "net total", "total"),
    "subtotal": ("subtotal", "sub-total", "sub total", "gross amount"),
}

# Lines that are never items.
_NOISE = re.compile(
    r"tin\b|vat reg|bir|serial|accr|permit|machine|terminal|cashier|thank|"
    r"welcome|official receipt|invoice|osca|pwd id|signature|change|cash|"
    r"tendered|customer|address|tel|contact|senior citizen id|www\.|@",
    re.I,
)

_NON_VAT = re.compile(r"non[\s-]?vat|vat[\s-]?exempt taxpayer|percentage tax", re.I)

_ESTABLISHMENTS: dict[str, tuple[str, ...]] = {
    "pharmacy": ("pharmacy", "drug", "botika", "mercury", "watsons",
                 "generika", "rose pharmacy", "southstar"),
    "restaurant": ("restaurant", "carinderia", "cafe", "coffee", "grill",
                   "food", "kitchen", "resto", "jollibee", "mcdonald",
                   "chowking", "mang inasal", "greenwich", "kfc", "bakery",
                   "panaderia", "eatery", "canteen", "lugawan"),
    "grocery": ("supermarket", "grocery", "mart", "puregold", "savemore",
                "robinsons", "waltermart", "landers", "s&r", "sari-sari",
                "palengke", "market"),
    "clinic": ("clinic", "hospital", "medical center", "laboratory",
               "diagnostic", "dental", "ospital"),
    "cinema": ("cinema", "theater", "theatre", "cineplex"),
    "transport": ("bus", "transit", "liner", "airlines", "ferry", "shipping"),
}


def _to_amount(raw: str) -> Decimal | None:
    # Real money always contains at least one true digit. Checking before the
    # OCR repair keeps "P.OS" from being rewritten into "0.05" and booked as
    # a line item.
    if not any(c.isdigit() for c in raw):
        return None

    text = raw.translate(_DIGIT_FIXES).strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")

    tail = _DECIMAL_TAIL.search(text)
    if tail:
        whole = text[: tail.start()].replace(",", "").replace(".", "")
        text = f"{whole}.{tail.group(1)}"
    else:
        text = text.replace(",", "")

    try:
        amount = peso(text)
    except (ValueError, TypeError):
        return None
    return -amount if negative else amount


# Needles flattened and sorted by length so the LONGEST match wins. Grouping by
# field and comparing group maxima is not enough: "total" is a substring of
# "subtotal", so a SUBTOTAL line was being recorded as the receipt total, and a
# receipt whose AMOUNT DUE failed to OCR would then be audited against its
# subtotal and declared correct.
_NEEDLES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((needle, field) for field, needles in _LABELS.items() for needle in needles),
        key=lambda pair: -len(pair[0]),
    )
)


def _label_of(line: str) -> str | None:
    low = line.lower()
    for needle, field_name in _NEEDLES:
        if needle in low:
            return field_name
    return None


def establishment_of(text: str) -> str:
    """Guess the kind of establishment from the receipt header.

    Only the first dozen lines are considered: that is where the trade name
    sits, and item names further down would otherwise vote.
    """
    head = "\n".join(text.splitlines()[:12]).lower()
    for kind, needles in _ESTABLISHMENTS.items():
        if any(n in head for n in needles):
            return kind
    return "unknown"


# --------------------------------------------------------------------------
# Item classification
# --------------------------------------------------------------------------

_KEYWORDS: tuple[tuple[Category, tuple[str, ...]], ...] = (
    (Category.ALCOHOL, ("beer", "red horse", "san mig", "gin ", "rum",
                        "brandy", "whisky", "whiskey", "vodka", "wine",
                        "emperador", "tanduay", "lambanog", "alak", "liquor")),
    (Category.TOBACCO, ("cigarette", "sigarilyo", "marlboro", "winston",
                        "fortune", "mighty", "hope ", "vape", "tobacco")),
    # No bare "tab" or "mg" here: "tab" matches VEGETABLE and would grant a
    # grocery vegetable the 20% + VAT exemption instead of the 5%, which is
    # an over-claim. Dosages are matched by _DOSAGE below instead.
    (Category.MEDICINE, (" tablet", " tabs", " caps", " capsule", " syrup",
                         "suspension", "ointment", "gamot", "paracetamol",
                         "biogesic", "neozep", "amlodipine", "losartan",
                         "metformin", "atorvastatin", "salbutamol",
                         "amoxicillin", "cetirizine", "insulin", "aspirin",
                         "antibiotic", "maintenance")),
    (Category.LAB_DIAGNOSTIC, ("x-ray", "xray", "cbc", "urinalysis",
                               "ultrasound", "ecg", "blood", "laboratory",
                               "fbs", "lipid", "swab", "biopsy")),
    (Category.MEDICAL_SERVICE, ("consultation", "konsulta", "check-up",
                                "checkup", "dental", "tooth", "extraction",
                                "professional fee", "pf ", "doctor")),
    (Category.FUNERAL, ("funeral", "burial", "memorial", "casket", "cremation",
                        "libing")),
    (Category.CINEMA_THEATER, ("cinema", "movie", "admission", "theater")),
    (Category.TRANSPORT_AIR_SEA, ("airfare", "flight", "ferry", "baggage")),
    (Category.TRANSPORT_LAND, ("fare", "pamasahe", "bus ticket", "toll")),
    (Category.HOTEL_LODGING, ("room night", "lodging", "accommodation",
                              "check-in")),
    (Category.RESTAURANT, ("meal", "combo", "rice ", "kanin", "adobo",
                           "sinigang", "lugaw", "pancit", "lomi", "siomai",
                           "burger", "fries", "chicken", "manok", "coffee",
                           "kape", "iced tea", "softdrink", "soda", "halo",
                           "sundae", "spaghetti", "pizza", "tapsilog",
                           "silog", "ulam", "merienda", "bbq", "lechon")),
    (Category.BASIC_NECESSITY, ("bigas", "rice", "itlog", "egg", "gatas",
                                "milk", "tinapay", "bread", "pandesal",
                                "sardinas", "sardines", "noodle", "asukal",
                                "sugar", "asin", "salt", "mantika",
                                "cooking oil", "coffee refill", "dilis",
                                "tuyo", "bottled water", "candle", "kandila")),
    (Category.PRIME_COMMODITY, ("detergent", "sabon", "soap", "shampoo",
                                "toothpaste", "batteries", "bateria", "flour",
                                "harina", "margarine", "cheese", "ham",
                                "tissue", "diaper", "lpg", "gasul")),
)

# When no keyword matches, what the establishment implies. The unknown case is
# deliberately the no-discount default.
_FALLBACK: dict[str, Category] = {
    "pharmacy": Category.MEDICINE,
    "restaurant": Category.RESTAURANT,
    "clinic": Category.MEDICAL_SERVICE,
    "cinema": Category.CINEMA_THEATER,
    "transport": Category.TRANSPORT_LAND,
    "grocery": Category.OTHER_GOODS,
    "unknown": Category.OTHER_GOODS,
}

# Items whose category flips the discount depending on where you bought them.
_CONTEXT_SENSITIVE = ("rice", "coffee", "kape", "bread", "tinapay", "water",
                      "milk", "gatas", "egg", "itlog")


# A dosage strength is the most reliable medicine signal on a receipt line
# ("LOSARTAN 50MG", "AMOXICILLIN 250 MG/5ML"). Anchored to a digit so it does
# not fire on ordinary words.
_DOSAGE = re.compile(r"\d+\s*(?:mg|mcg|iu)\b", re.I)


def classify(description: str, establishment: str = "unknown") -> tuple[Category, bool]:
    """Guess a line's category. Returns ``(category, needs_confirmation)``."""
    low = f" {description.lower()} "

    if _DOSAGE.search(low):
        return Category.MEDICINE, False

    ambiguous = any(token in low for token in _CONTEXT_SENSITIVE)
    if ambiguous and establishment in ("restaurant", "grocery"):
        # The establishment settles it, and this is exactly the case where
        # guessing wrong costs 15 percentage points of discount.
        return (
            Category.RESTAURANT
            if establishment == "restaurant"
            else Category.BASIC_NECESSITY
        ), False

    for category, needles in _KEYWORDS:
        if any(n in low for n in needles):
            return category, False

    return _FALLBACK[establishment], establishment == "unknown"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@dataclass
class ParsedReceipt:
    receipt: Receipt
    establishment: str
    needs_confirmation: list[str] = field(default_factory=list)
    fields_found: dict[str, Decimal] = field(default_factory=dict)
    raw_text: str = ""


def parse(
    text: str,
    *,
    cardholder: Cardholder = Cardholder.SENIOR,
    establishment: str | None = None,
    diners_sharing: int = 1,
) -> ParsedReceipt:
    """Read OCR text into a receipt, flagging everything uncertain."""
    kind = establishment or establishment_of(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    found: dict[str, Decimal] = {}
    items: list[LineItem] = []
    confirm: list[str] = []
    merchant = ""

    for raw in lines:
        if not merchant and not _NOISE.search(raw) and not _MONEY_AT_END.search(raw):
            if len(raw) > 3 and any(c.isalpha() for c in raw):
                merchant = raw.strip(" *=-")

        match = _MONEY_AT_END.search(raw)
        if not match:
            continue
        amount = _to_amount(match.group(1))
        if amount is None:
            continue

        label = _label_of(raw)
        if label:
            # Labels can repeat; keep the last, which is usually the final
            # figure on a receipt with running subtotals.
            found[label] = amount
            continue

        description = raw[: match.start()].strip(" .:*-\t")
        if not description or _NOISE.search(description):
            continue
        # Strip a leading quantity like "2 x " or "3 @".
        description = re.sub(r"^\d+\s*[x@]\s*", "", description, flags=re.I)
        if not description:
            continue

        category, uncertain = classify(description, kind)
        if uncertain:
            confirm.append(
                f"Hindi tiyak ang kategorya ng {description!r}; "
                f"itinuring na walang diskwento."
            )
        items.append(
            LineItem(
                description=description,
                gross=amount,
                category=category,
                diners_sharing=diners_sharing,
            )
        )

    if not items:
        raise ValueError(
            "No item lines could be read from the receipt. Retake the photo "
            "square to the paper, filling the frame, or enter the amounts by "
            "hand."
        )

    # Cross-check: items should roughly sum to the subtotal if one was read.
    subtotal = found.get("subtotal")
    if subtotal is not None:
        total_items = sum((i.gross for i in items), ZERO)
        if abs(total_items - subtotal) > Decimal("1.00"):
            confirm.append(
                f"Ang suma ng mga item ({total_items}) ay hindi tugma sa "
                f"subtotal sa resibo ({subtotal}); may item na hindi nabasa."
            )

    printed_total = found.get("total")
    if printed_total is None:
        confirm.append(
            "Hindi nabasa ang total sa resibo; ilagay po ito nang manu-mano."
        )

    receipt = Receipt(
        cardholder=cardholder,
        lines=tuple(items),
        seller_vat_registered=not bool(_NON_VAT.search(text)),
        service_charge=found.get("service_charge", ZERO),
        printed_total=printed_total,
        printed_discount=found.get("discount"),
        printed_vat=found.get("vat"),
        merchant=merchant,
    )
    return ParsedReceipt(
        receipt=receipt,
        establishment=kind,
        needs_confirmation=confirm,
        fields_found=found,
        raw_text=text,
    )
