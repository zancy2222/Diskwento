"""Parser tests against realistic receipt text.

The parser is the pipeline's weakest stage, so what is asserted here is less
"it gets everything right" and more "when it is unsure it says so, and when it
guesses it guesses toward claiming LESS". A false 'you were shortchanged'
sends a senior citizen to argue with a manager who turns out to be right.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from diskwento.engine import audit
from diskwento.law import Cardholder, Category
from diskwento.parser import classify, establishment_of, parse

CARINDERIA = """\
ALING NENA CARINDERIA
123 Rizal St, Taytay, Rizal
TIN 123-456-789-000
OFFICIAL RECEIPT
OR-0042   02/14/2026

LUGAW NA MAY ITLOG        60.00
TOKWA'T BABOY             85.00
KAPE                      25.00
SUBTOTAL                 170.00
SC DISC                   34.00
VAT 12%                   14.57
AMOUNT DUE               136.00
CASH                     150.00
CHANGE                    14.00
THANK YOU PO!
"""

SARI_SARI = """\
TINDAHAN NI ALING ROSA
NON-VAT REGISTERED
SARDINAS 155G             30.00
TOTAL                     30.00
"""


class TestEstablishment(unittest.TestCase):
    def test_header_decides_the_establishment(self):
        self.assertEqual(establishment_of(CARINDERIA), "restaurant")
        self.assertEqual(establishment_of("MERCURY DRUG\nITEM 10.00"), "pharmacy")
        self.assertEqual(establishment_of("PUREGOLD TAYTAY\nITEM 10.00"), "grocery")
        self.assertEqual(establishment_of("ACME CORP\nITEM 10.00"), "unknown")

    def test_only_the_header_votes(self):
        """An item named 'pharmacy bag' far down must not retype the store."""
        text = "PUREGOLD\n" + "\n".join(f"ITEM {i} 1.00" for i in range(20))
        text += "\nPHARMACY BAG 5.00"
        self.assertEqual(establishment_of(text), "grocery")


class TestClassification(unittest.TestCase):
    def test_dosage_strength_identifies_medicine(self):
        self.assertEqual(classify("LOSARTAN 50MG")[0], Category.MEDICINE)
        self.assertEqual(classify("AMOXICILLIN 250 MG")[0], Category.MEDICINE)

    def test_vegetable_is_not_medicine(self):
        """'tab' inside VEGETABLE used to grant 20% + VAT exemption."""
        category, _ = classify("MIXED VEGETABLES", "grocery")
        self.assertNotEqual(category, Category.MEDICINE)

    def test_context_resolves_rice(self):
        self.assertEqual(classify("RICE", "restaurant")[0], Category.RESTAURANT)
        self.assertEqual(classify("RICE", "grocery")[0], Category.BASIC_NECESSITY)

    def test_unknown_item_in_unknown_store_claims_nothing(self):
        category, confirm = classify("ZXQ-4471", "unknown")
        self.assertEqual(category, Category.OTHER_GOODS)
        self.assertTrue(confirm)

    def test_alcohol_and_tobacco_are_caught(self):
        self.assertEqual(classify("RED HORSE 1L", "grocery")[0], Category.ALCOHOL)
        self.assertEqual(classify("MARLBORO RED", "grocery")[0], Category.TOBACCO)


class TestParse(unittest.TestCase):
    def test_reads_a_carinderia_receipt_end_to_end(self):
        parsed = parse(CARINDERIA)
        receipt = parsed.receipt

        self.assertEqual(parsed.establishment, "restaurant")
        self.assertEqual(receipt.merchant, "ALING NENA CARINDERIA")
        self.assertEqual(len(receipt.lines), 3)
        self.assertEqual(receipt.printed_total, Decimal("136.00"))
        self.assertEqual(receipt.printed_discount, Decimal("34.00"))
        self.assertEqual(receipt.printed_vat, Decimal("14.57"))
        self.assertTrue(receipt.seller_vat_registered)
        self.assertEqual(parsed.needs_confirmation, [])

        # And the audit of what was parsed agrees with the hand-built fixture.
        result = audit(receipt)
        self.assertEqual(result.expected_total, Decimal("121.43"))
        self.assertEqual(result.overcharge, Decimal("14.57"))
        self.assertEqual(result.diagnosis.pattern, "VAT_NOT_EXEMPTED")

    def test_cash_and_change_are_not_items(self):
        descriptions = [ln.description for ln in parse(CARINDERIA).receipt.lines]
        joined = " ".join(descriptions).lower()
        self.assertNotIn("cash", joined)
        self.assertNotIn("change", joined)
        self.assertNotIn("subtotal", joined)

    def test_non_vat_registration_is_detected(self):
        parsed = parse(SARI_SARI)
        self.assertFalse(parsed.receipt.seller_vat_registered)

    def test_missing_item_is_reported_via_the_subtotal_cross_check(self):
        text = CARINDERIA.replace("TOKWA'T BABOY             85.00\n", "")
        parsed = parse(text)
        self.assertTrue(
            any("hindi tugma sa subtotal" in n for n in parsed.needs_confirmation)
        )

    def test_unreadable_total_is_flagged_not_guessed(self):
        text = CARINDERIA.replace("AMOUNT DUE               136.00", "")
        parsed = parse(text)
        self.assertIsNone(parsed.receipt.printed_total)
        self.assertTrue(
            any("total" in n.lower() for n in parsed.needs_confirmation)
        )

    def test_quantity_prefix_is_stripped(self):
        parsed = parse("ALING NENA CARINDERIA\n2 x PANCIT BIHON   120.00\n"
                       "AMOUNT DUE 120.00\n")
        self.assertEqual(parsed.receipt.lines[0].description, "PANCIT BIHON")

    def test_ocr_digit_confusion_is_repaired(self):
        """O-for-zero and S-for-5 are the classic thermal-receipt misreads."""
        parsed = parse("ALING NENA CARINDERIA\nLUGAW   6O.OO\n"
                       "AMOUNT DUE   6O.OO\n")
        self.assertEqual(parsed.receipt.lines[0].gross, Decimal("60.00"))

    def test_a_receipt_with_no_readable_items_raises_rather_than_audits(self):
        with self.assertRaises(ValueError):
            parse("SOME STORE\nTHANK YOU\n")

    def test_diners_sharing_is_threaded_through(self):
        parsed = parse(CARINDERIA, diners_sharing=4)
        self.assertTrue(all(ln.diners_sharing == 4 for ln in parsed.receipt.lines))

    def test_cardholder_is_threaded_through(self):
        parsed = parse(CARINDERIA, cardholder=Cardholder.PWD)
        self.assertEqual(parsed.receipt.cardholder, Cardholder.PWD)


if __name__ == "__main__":
    unittest.main()
