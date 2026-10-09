"""Tests for the model stage: the hallucination guard and the fallback.

No Ollama required -- these exercise verification and the template path, which
are the parts that have to be right when the model misbehaves.
"""

from __future__ import annotations

import unittest

from diskwento.engine import LineItem, Receipt, audit, to_brief
from diskwento.law import Cardholder, Category
from diskwento.llm import _norm, template, verify, write_up


def _brief(gross: str = "112.00", printed: str | None = "89.60") -> dict:
    return to_brief(
        audit(
            Receipt(
                cardholder=Cardholder.SENIOR,
                lines=(LineItem("Pancit", gross, Category.RESTAURANT),),
                printed_total=printed,
                merchant="Aling Nena Carinderia",
                date="2026-02-14",
                receipt_no="0042",
            )
        )
    )


class TestNormalization(unittest.TestCase):
    def test_thousands_do_not_collide_with_tens(self):
        """Trailing-zero trimming would make these equal and break the guard."""
        self.assertNotEqual(_norm("1,300"), _norm("13"))
        self.assertEqual(_norm("1,300"), _norm("1300.00"))
        self.assertEqual(_norm("80"), _norm("80.00"))


class TestGuard(unittest.TestCase):
    def test_the_template_passes_its_own_guard(self):
        """The fallback must satisfy the rules we hold the model to."""
        for printed in ("89.60", "80.00", "71.43", None):
            brief = _brief(printed=printed)
            with self.subTest(status=brief["status"]):
                self.assertTrue(verify(template(brief), brief).ok)

    def test_invented_amount_is_rejected(self):
        brief = _brief()
        draft = template(brief)
        draft["summary_tl"] += " May sobra pong ₱999.99."
        verdict = verify(draft, brief)
        self.assertFalse(verdict.ok)
        self.assertTrue(any("invented peso amounts" in r for r in verdict.reasons))

    def test_invented_republic_act_is_rejected(self):
        brief = _brief()
        draft = template(brief)
        draft["summary_tl"] += " Ayon din sa Republic Act No. 11111."
        verdict = verify(draft, brief)
        self.assertFalse(verdict.ok)
        self.assertTrue(any("Republic Acts" in r for r in verdict.reasons))

    def test_citation_actually_in_the_brief_is_accepted(self):
        brief = _brief()
        draft = template(brief)
        draft["summary_tl"] += " Ayon sa RA 9994, may karapatan po kayo."
        self.assertTrue(verify(draft, brief).ok)

    def test_complaint_on_a_correct_receipt_is_rejected(self):
        brief = _brief(printed="80.00")
        self.assertEqual(brief["status"], "CORRECT")
        draft = template(brief)
        draft["complaint_tl"] = "Sa Butihing Manager, may reklamo po ako."
        self.assertFalse(verify(draft, brief).ok)

    def test_missing_keys_are_rejected(self):
        brief = _brief()
        self.assertFalse(verify({"headline_tl": "Kulang po."}, brief).ok)

    def test_empty_headline_is_rejected(self):
        brief = _brief()
        draft = template(brief)
        draft["headline_tl"] = "   "
        self.assertFalse(verify(draft, brief).ok)


class TestTemplate(unittest.TestCase):
    def test_shortchanged_letter_carries_the_essentials(self):
        brief = _brief()
        letter = template(brief)["complaint_tl"]
        self.assertIsNotNone(letter)
        self.assertIn("Aling Nena Carinderia", letter)
        self.assertIn("0042", letter)
        self.assertIn("₱9.60", letter)
        self.assertIn("Republic Act No. 9994", letter)
        self.assertIn("OSCA o PWD ID", letter)

    def test_correct_receipt_has_no_letter(self):
        self.assertIsNone(template(_brief(printed="80.00"))["complaint_tl"])

    def test_pwd_letter_uses_pwd_wording(self):
        brief = to_brief(
            audit(
                Receipt(
                    cardholder=Cardholder.PWD,
                    lines=(LineItem("Losartan", "560.00", Category.MEDICINE),),
                    printed_total="448.00",
                    merchant="Botika",
                )
            )
        )
        letter = template(brief)["complaint_tl"]
        self.assertIn("person with disability", letter)
        self.assertIn("10754", letter)


class TestWriteUp(unittest.TestCase):
    def test_skipping_the_model_still_produces_output(self):
        out = write_up(_brief(), use_model=False)
        self.assertEqual(out.source, "template")
        self.assertTrue(out.complaint_tl)

    def test_unreachable_ollama_falls_back_rather_than_raising(self):
        out = write_up(_brief(), host="http://127.0.0.1:1", timeout=1.0)
        self.assertEqual(out.source, "template")
        self.assertTrue(out.headline_tl)
        self.assertTrue(any("template" in n for n in out.notes))


if __name__ == "__main__":
    unittest.main()
