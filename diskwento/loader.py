"""Receipt JSON to engine types.

Used by the CLI and by whatever UI you put on top tomorrow. Amounts stay as
strings all the way in -- JSON floats would reintroduce binary rounding into
the one part of the app that must not have it.
"""

from __future__ import annotations

import json
from pathlib import Path

from .engine import LineItem, Receipt
from .law import Cardholder, Category


def receipt_from_dict(data: dict) -> Receipt:
    cardholder = Cardholder(str(data.get("cardholder", "senior")).lower())
    lines = tuple(
        LineItem(
            description=item["description"],
            gross=item["gross"],
            category=Category(str(item.get("category", "other_goods")).lower()),
            diners_sharing=int(item.get("diners_sharing", 1)),
            promo_discount=item.get("promo_discount", "0.00"),
        )
        for item in data["lines"]
    )
    return Receipt(
        cardholder=cardholder,
        lines=lines,
        seller_vat_registered=bool(data.get("seller_vat_registered", True)),
        service_charge=data.get("service_charge", "0.00"),
        printed_total=data.get("printed_total"),
        printed_discount=data.get("printed_discount"),
        printed_vat=data.get("printed_vat"),
        basic_necessity_spent_this_week=data.get(
            "basic_necessity_spent_this_week", "0.00"
        ),
        merchant=data.get("merchant", ""),
        receipt_no=data.get("receipt_no", ""),
        date=data.get("date", ""),
    )


def receipt_from_json(path: str | Path) -> Receipt:
    return receipt_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
