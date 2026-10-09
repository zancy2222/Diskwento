"""Peso arithmetic.

Every amount in this project is a ``Decimal`` quantized to centavos.

Floats are banned here. ``0.1 + 0.2 != 0.3``, and an auditor that is off by a
centavo is worse than no auditor at all -- it hands the cashier a reason to
dismiss the customer. BIR practice is to round to the centavo, half up, so
that is what :func:`q` does.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENTAVO = Decimal("0.01")
ZERO = Decimal("0.00")

# Strips peso signs, thousands separators, stray spaces and OCR noise before
# we try to read a number. OCR loves turning "1,234.56" into "P1 ,234. 56".
_CLEAN = re.compile(r"[^0-9.\-]")


def peso(value: object) -> Decimal:
    """Coerce ``value`` to a centavo-quantized ``Decimal``.

    Accepts ``Decimal``, ``int``, or a string straight off an OCR pass
    (``"P1,234.56"``, ``"(45.00)"`` for a negative, ``"1 234.56"``).
    Raises ``ValueError`` on anything we cannot read, because silently
    coercing garbage to zero is how an auditor reports "walang problema"
    on a receipt it never understood.
    """
    if isinstance(value, Decimal):
        return q(value)
    if isinstance(value, int):
        return q(Decimal(value))
    if isinstance(value, float):
        raise TypeError(
            f"refusing to read the float {value!r} as money; "
            "pass a str or Decimal so no binary rounding sneaks in"
        )
    if value is None:
        raise ValueError("cannot read None as an amount")

    raw = str(value).strip()
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = _CLEAN.sub("", raw)
    if cleaned in ("", "-", ".", "-."):
        raise ValueError(f"cannot read {value!r} as an amount")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise ValueError(f"cannot read {value!r} as an amount") from exc
    return q(-amount if negative else amount)


def q(value: Decimal) -> Decimal:
    """Round to the centavo, half up -- the convention BIR receipts use."""
    return value.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def fmt(value: Decimal) -> str:
    """Format for a receipt or a complaint letter: ``PHP 1,234.56``."""
    return f"₱{value:,.2f}"


def pct(value: Decimal) -> str:
    """Format a rate the way a letter would read it: ``20%``, ``12.5%``."""
    scaled = value * 100
    whole = scaled == scaled.to_integral_value()
    return f"{scaled.to_integral_value()}%" if whole else f"{scaled.normalize()}%"
