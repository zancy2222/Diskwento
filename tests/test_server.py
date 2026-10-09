"""Tests for the local web GUI's HTTP layer.

Runs a real server on an ephemeral port and talks to it over HTTP, so the
routing, JSON encoding and error handling are exercised the way the page uses
them. No Ollama and no OCR required: the server is started with the model
disabled, which is also how it behaves on a machine with neither installed.
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from diskwento import llm
from diskwento.engine import LineItem, Receipt, audit, to_brief
from diskwento.law import Cardholder, Category
from diskwento.server import Handler, payload


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Handler.options = {
            "model": llm.DEFAULT_MODEL,
            "host": llm.DEFAULT_HOST,
            "use_model": False,
            "quiet": True,
        }
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=20) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")

    def post(self, path, body):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as r:
            return r.status, json.loads(r.read())

    def post_expecting_error(self, path, body):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(path, body)
        return caught.exception.code, json.loads(caught.exception.read())


class TestPages(ServerTestCase):
    def test_index_is_served_as_utf8_html(self):
        status, body, content_type = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("charset=utf-8", content_type)
        self.assertIn(b"Tama ba ang Diskwento?", body)

    def test_health_reports_what_is_installed(self):
        _, body, _ = self.get("/api/health")
        data = json.loads(body)
        self.assertIn("ocr_backends", data)
        self.assertIn("ollama_ok", data)

    def test_samples_are_listed(self):
        _, body, _ = self.get("/api/samples")
        samples = json.loads(body)["samples"]
        self.assertEqual(len(samples), 6)
        self.assertTrue(all(s["name"].endswith(".json") for s in samples))

    def test_unknown_path_is_a_json_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nope")
        self.assertEqual(caught.exception.code, 404)


class TestAudit(ServerTestCase):
    def test_sample_audit_round_trip(self):
        _, data = self.post(
            "/api/audit", {"sample": "01_carinderia_vat_not_removed.json"}
        )
        self.assertEqual(data["status"], "SHORTCHANGED")
        self.assertEqual(data["overcharge"], "₱14.57")
        self.assertEqual(data["expected_total"], "₱121.43")
        self.assertEqual(data["diagnosis"]["pattern"], "VAT_NOT_EXEMPTED")
        self.assertTrue(data["writeup"]["complaint_tl"])
        self.assertTrue(data["citations"])

    def test_manual_receipt_round_trip(self):
        _, data = self.post(
            "/api/audit",
            {
                "receipt": {
                    "cardholder": "senior",
                    "merchant": "Test",
                    "lines": [
                        {
                            "description": "Pancit",
                            "gross": "112.00",
                            "category": "restaurant",
                        }
                    ],
                    "printed_total": "112.00",
                }
            },
        )
        self.assertEqual(data["status"], "SHORTCHANGED")
        self.assertEqual(data["overcharge"], "₱32.00")
        self.assertEqual(data["diagnosis"]["pattern"], "NO_PRIVILEGE_APPLIED")

    def test_correct_receipt_has_no_letter(self):
        _, data = self.post("/api/audit", {"sample": "03_tamang_resibo.json"})
        self.assertEqual(data["status"], "CORRECT")
        self.assertIsNone(data["writeup"]["complaint_tl"])

    def test_amounts_are_preformatted_strings(self):
        """The page never does arithmetic, so it must never receive raw
        numbers to format."""
        _, data = self.post("/api/audit", {"sample": "02_botika_walang_diskwento.json"})
        for key in ("expected_total", "overcharge", "printed_total"):
            self.assertIsInstance(data[key], str)
            self.assertTrue(data[key].startswith("₱"), data[key])


class TestErrors(ServerTestCase):
    def test_sample_name_cannot_escape_the_samples_directory(self):
        code, data = self.post_expecting_error(
            "/api/audit", {"sample": "../../etc/passwd"}
        )
        self.assertEqual(code, 400)
        self.assertIn("walang halimbawang", data["error"])

    def test_absolute_sample_path_is_refused(self):
        code, _ = self.post_expecting_error("/api/audit", {"sample": "/etc/passwd"})
        self.assertEqual(code, 400)

    def test_empty_body_is_rejected(self):
        code, data = self.post_expecting_error("/api/audit", {})
        self.assertEqual(code, 400)
        self.assertIn("receipt", data["error"])

    def test_receipt_with_no_lines_is_rejected(self):
        code, _ = self.post_expecting_error(
            "/api/audit", {"receipt": {"cardholder": "senior", "lines": []}}
        )
        self.assertEqual(code, 400)

    def test_scan_without_an_image_is_rejected(self):
        code, data = self.post_expecting_error("/api/scan", {})
        self.assertEqual(code, 400)
        self.assertIn("litrato", data["error"])

    def test_scan_with_unreadable_base64_is_rejected(self):
        code, _ = self.post_expecting_error("/api/scan", {"image": "not base64!!"})
        self.assertEqual(code, 400)


class TestPayload(unittest.TestCase):
    def test_payload_is_json_serializable(self):
        """Decimals would raise here; every amount must already be a string."""
        result = audit(
            Receipt(
                cardholder=Cardholder.SENIOR,
                lines=(LineItem("Pancit", "112.00", Category.RESTAURANT),),
                printed_total="89.60",
            )
        )
        writeup = llm.write_up(to_brief(result), use_model=False)
        encoded = json.dumps(payload(result, writeup), ensure_ascii=False)
        self.assertIn("₱", encoded)


if __name__ == "__main__":
    unittest.main()
