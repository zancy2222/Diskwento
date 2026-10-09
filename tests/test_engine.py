"""Engine tests.

Plain ``unittest`` so this runs with zero installs:

    python -m unittest discover -s tests -v

The cases below are the receipts we expect to meet at a checkout counter. If
one of these regresses, the app is telling someone they were not shortchanged
when they were, so treat a red test here as a demo blocker.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from diskwento.engine import (
    CANNOT_VERIFY,
    CORRECT,
    LineItem,
    OVER_DISCOUNTED,
    Receipt,
    SHORTCHANGED,
    audit,
    to_brief,
)
from diskwento.law import Cardholder, Category


def _restaurant(gross: str, printed: str | None, **kw) -> Receipt:
    return Receipt(
        cardholder=Cardholder.SENIOR,
        lines=(LineItem("Lugaw at tinapay", gross, Category.RESTAURANT),),
        printed_total=printed,
        **kw,
    )


class TestCoreComputation(unittest.TestCase):
    """PHP 112.00 of covered goods must be billed at PHP 80.00."""

    def test_textbook_case(self):
        a = audit(_restaurant("112.00", "80.00"))
        self.assertEqual(a.expected_vat_exempt_base, Decimal("100.00"))
        self.assertEqual(a.expected_vat_removed, Decimal("12.00"))
        self.assertEqual(a.expected_discount, Decimal("20.00"))
        self.assertEqual(a.expected_total, Decimal("80.00"))
        self.assertEqual(a.status, CORRECT)
        self.assertEqual(a.diagnosis.pattern, "CORRECT")

    def test_effective_reduction_is_28_57_percent_not_20(self):
        """The whole point of the app: 20% off the shelf price is not enough."""
        a = audit(_restaurant("1000.00", None))
        self.assertEqual(a.expected_total, Decimal("714.29"))
        naive_twenty_percent = Decimal("800.00")
        self.assertGreater(naive_twenty_percent, a.expected_total)

    def test_no_privilege_applied_at_all(self):
        a = audit(_restaurant("112.00", "112.00"))
        self.assertEqual(a.status, SHORTCHANGED)
        self.assertEqual(a.overcharge, Decimal("32.00"))
        self.assertEqual(a.diagnosis.pattern, "NO_PRIVILEGE_APPLIED")

    def test_vat_never_removed(self):
        """The most common real-world error: 20% off the VAT-inclusive price."""
        a = audit(_restaurant("112.00", "89.60"))
        self.assertEqual(a.status, SHORTCHANGED)
        self.assertEqual(a.overcharge, Decimal("9.60"))
        self.assertEqual(a.diagnosis.pattern, "VAT_NOT_EXEMPTED")

    def test_vat_exempted_but_no_discount(self):
        a = audit(_restaurant("112.00", "100.00"))
        self.assertEqual(a.status, SHORTCHANGED)
        self.assertEqual(a.overcharge, Decimal("20.00"))
        self.assertEqual(a.diagnosis.pattern, "VAT_EXEMPT_ONLY")

    def test_wrong_rate_on_correct_base(self):
        a = audit(_restaurant("112.00", "95.00"))
        self.assertEqual(a.status, SHORTCHANGED)
        self.assertEqual(a.diagnosis.pattern, "WRONG_RATE_ON_BASE:0.05")

    def test_over_discount_is_flagged_but_not_a_complaint(self):
        a = audit(_restaurant("112.00", "71.43"))
        self.assertEqual(a.status, OVER_DISCOUNTED)
        self.assertFalse(a.shortchanged)

    def test_unreadable_total_does_not_produce_a_verdict(self):
        a = audit(_restaurant("112.00", None))
        self.assertEqual(a.status, CANNOT_VERIFY)
        self.assertIsNone(a.diagnosis)
        self.assertEqual(a.expected_total, Decimal("80.00"))

    def test_centavo_rounding_is_tolerated(self):
        """A POS that rounds differently must not be called a thief."""
        a = audit(_restaurant("99.99", "71.42"))
        self.assertEqual(a.status, CORRECT)


class TestNonVatSeller(unittest.TestCase):
    def test_no_vat_to_strip(self):
        a = audit(_restaurant("100.00", "80.00", seller_vat_registered=False))
        self.assertEqual(a.expected_vat_removed, Decimal("0.00"))
        self.assertEqual(a.expected_discount, Decimal("20.00"))
        self.assertEqual(a.expected_total, Decimal("80.00"))
        self.assertEqual(a.status, CORRECT)


class TestEligibility(unittest.TestCase):
    def test_alcohol_and_tobacco_carry_no_discount(self):
        a = audit(
            Receipt(
                cardholder=Cardholder.SENIOR,
                lines=(
                    LineItem("Red Horse", "120.00", Category.ALCOHOL),
                    LineItem("Marlboro", "180.00", Category.TOBACCO),
                ),
                printed_total="300.00",
            )
        )
        self.assertEqual(a.expected_discount, Decimal("0.00"))
        self.assertEqual(a.status, CORRECT)

    def test_mixed_basket_discounts_only_the_covered_lines(self):
        a = audit(
            Receipt(
                cardholder=Cardholder.PWD,
                lines=(
                    LineItem("Losartan 50mg", "560.00", Category.MEDICINE),
                    LineItem("Shampoo", "150.00", Category.OTHER_GOODS),
                ),
                printed_total="550.00",
            )
        )
        # 560 / 1.12 = 500; less 20% = 400; plus 150 untouched = 550.
        self.assertEqual(a.expected_total, Decimal("550.00"))
        self.assertEqual(a.status, CORRECT)
        self.assertEqual(a.covered_gross, Decimal("560.00"))


class TestBasicNecessities(unittest.TestCase):
    def test_five_percent_with_no_vat_exemption(self):
        a = audit(
            Receipt(
                cardholder=Cardholder.SENIOR,
                lines=(LineItem("Bigas 5kg", "1000.00", Category.BASIC_NECESSITY),),
                printed_total="950.00",
            )
        )
        self.assertEqual(a.expected_discount, Decimal("50.00"))
        self.assertEqual(a.expected_vat_removed, Decimal("0.00"))
        self.assertEqual(a.status, CORRECT)

    def test_omitted_five_percent_is_diagnosed_not_shrugged_off(self):
        """A grocery that ignores the 5% must be named, not reported as
        'UNRECOGNIZED' -- the error lives outside the 20% portion."""
        a = audit(
            Receipt(
                cardholder=Cardholder.SENIOR,
                lines=(
                    LineItem("Bigas 25kg", "1500.00", Category.BASIC_NECESSITY),
                    LineItem("Paracetamol", "56.00", Category.MEDICINE),
                ),
                printed_total="1540.00",
            )
        )
        # Rice caps at PHP 1,300 -> PHP 65.00; medicine 56 -> 40.00 correctly.
        self.assertEqual(a.status, SHORTCHANGED)
        self.assertEqual(a.overcharge, Decimal("65.00"))
        self.assertEqual(a.diagnosis.pattern, "BASIC_NECESSITY_DISCOUNT_MISSING")

    def test_cap_exhausted_line_adds_no_noise_to_the_math_trail(self):
        a = audit(
            Receipt(
                cardholder=Cardholder.SENIOR,
                lines=(
                    LineItem("Bigas", "1500.00", Category.BASIC_NECESSITY),
                    LineItem("Shampoo", "185.00", Category.PRIME_COMMODITY),
                ),
                printed_total="1620.00",
            )
        )
        self.assertFalse(any("₱0.00 × 5%" in s for s in a.steps))

    def test_weekly_ceiling_caps_the_discount(self):
        a = audit(
            Receipt(
                cardholder=Cardholder.SENIOR,
                lines=(LineItem("Bigas", "1000.00", Category.BASIC_NECESSITY),),
                basic_necessity_spent_this_week="1000.00",
                printed_total="985.00",
            )
        )
        # Only PHP 300.00 of headroom remains, so 5% of 300 = PHP 15.00.
        self.assertEqual(a.expected_discount, Decimal("15.00"))
        self.assertEqual(a.status, CORRECT)


class TestSharedBill(unittest.TestCase):
    def test_privilege_covers_only_the_cardholders_share(self):
        a = audit(
            Receipt(
                cardholder=Cardholder.SENIOR,
                lines=(
                    LineItem("Family set meal", "2000.00", Category.RESTAURANT,
                             diners_sharing=4),
                ),
                printed_total="1857.14",
            )
        )
        self.assertEqual(a.covered_gross, Decimal("500.00"))
        self.assertEqual(a.expected_total, Decimal("1857.14"))
        self.assertEqual(a.status, CORRECT)
        self.assertTrue(any("Hinati ang bill" in w for w in a.warnings))


class TestNoDoubleDiscount(unittest.TestCase):
    def test_higher_promo_wins(self):
        a = audit(
            Receipt(
                cardholder=Cardholder.SENIOR,
                lines=(
                    LineItem("Buffet (50% off promo)", "1120.00",
                             Category.RESTAURANT, promo_discount="560.00"),
                ),
                printed_total="560.00",
            )
        )
        # Statutory benefit would be 1120 - 800 = PHP 320.00; the promo of
        # PHP 560.00 is better, so the promo is what the law allows.
        self.assertEqual(a.expected_total, Decimal("560.00"))
        self.assertEqual(a.status, CORRECT)

    def test_statutory_wins_when_promo_is_smaller(self):
        a = audit(
            Receipt(
                cardholder=Cardholder.SENIOR,
                lines=(
                    LineItem("Meal (10% off promo)", "1120.00",
                             Category.RESTAURANT, promo_discount="112.00"),
                ),
                printed_total="800.00",
            )
        )
        self.assertEqual(a.expected_total, Decimal("800.00"))
        self.assertEqual(a.status, CORRECT)


class TestServiceCharge(unittest.TestCase):
    def test_service_charge_is_outside_the_discount_base(self):
        a = audit(
            Receipt(
                cardholder=Cardholder.SENIOR,
                lines=(LineItem("Pancit", "112.00", Category.RESTAURANT),),
                service_charge="11.20",
                printed_total="91.20",
            )
        )
        self.assertEqual(a.expected_total, Decimal("91.20"))
        self.assertEqual(a.status, CORRECT)
        self.assertTrue(any("service charge" in w for w in a.warnings))


class TestBrief(unittest.TestCase):
    def test_brief_is_all_preformatted_strings(self):
        brief = to_brief(audit(_restaurant("112.00", "89.60")))
        self.assertEqual(brief["status"], SHORTCHANGED)
        self.assertEqual(brief["amounts"]["correct_total"], "₱80.00")
        self.assertEqual(brief["amounts"]["overcharge"], "₱9.60")
        self.assertTrue(brief["citations"])
        for c in brief["citations"]:
            self.assertTrue(c["law"] and c["text_tl"])

    def test_floats_are_refused_outright(self):
        with self.assertRaises(TypeError):
            LineItem("Kanin", 112.00, Category.RESTAURANT)


if __name__ == "__main__":
    unittest.main()
