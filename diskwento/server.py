"""A local web GUI, on the standard library only.

Why a browser rather than a desktop toolkit: the browser already has a camera.
``getUserMedia`` and ``<input type="file" capture>`` work out of the box, where
a Tkinter build would need OpenCV wheels that are a reliable source of pain on
Windows. It also means the same page opens on a phone, which is where this app
actually belongs.

Still entirely local. The server binds 127.0.0.1 by default, every import is
standard library, and the only outbound connection anything here makes is to
Ollama on localhost.

    python -m diskwento serve

Images arrive as base64 inside a JSON body rather than multipart form data:
``canvas.toDataURL()`` hands us base64 anyway, and the ``cgi`` module that used
to parse multipart was removed in Python 3.13.
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import llm
from .engine import Audit, audit, to_brief
from .law import Cardholder
from .loader import receipt_from_dict
from .money import ZERO, fmt

_WEB = Path(__file__).parent / "web"
_SAMPLES = Path(__file__).resolve().parent.parent / "samples"
_MAX_BODY = 12 * 1024 * 1024  # a phone photo, with headroom
_DATA_URL = re.compile(r"^data:image/[a-zA-Z.+-]+;base64,")


_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
}


def _content_type(path: Path) -> str:
    return _TYPES.get(path.suffix.lower(), "application/octet-stream")


# --------------------------------------------------------------------------
# Shaping a result for the page
# --------------------------------------------------------------------------


def payload(result: Audit, writeup: llm.WriteUp) -> dict:
    r = result.receipt
    return {
        "status": result.status,
        "merchant": r.merchant,
        "date": r.date,
        "receipt_no": r.receipt_no,
        "cardholder": r.cardholder.value,
        "lines": [
            {
                "description": line.description,
                "gross": fmt(line.gross),
                "discount": fmt(line.discount) if line.discount > ZERO else None,
                "amount_due": fmt(line.amount_due),
                "category": line.category.value,
                "treatment": line.treatment.value,
                "notes": line.notes,
            }
            for line in result.lines
        ],
        "service_charge": fmt(r.service_charge) if r.service_charge > ZERO else None,
        "steps": result.steps,
        "expected_total": fmt(result.expected_total),
        "printed_total": fmt(r.printed_total) if r.printed_total is not None else None,
        "overcharge": fmt(abs(result.overcharge)),
        "warnings": result.warnings,
        "diagnosis": (
            {
                "pattern": result.diagnosis.pattern,
                "headline_tl": result.diagnosis.headline_tl,
                "detail_tl": result.diagnosis.detail_tl,
            }
            if result.diagnosis
            else None
        ),
        "citations": [
            {"law": c.law, "locus": c.locus, "text_tl": c.text_tl}
            for c in result.citations
        ],
        "writeup": {
            "headline_tl": writeup.headline_tl,
            "summary_tl": writeup.summary_tl,
            "ask_tl": writeup.ask_tl,
            "complaint_tl": writeup.complaint_tl,
            "source": writeup.source,
            "notes": writeup.notes,
        },
    }


def _run_audit(data: dict, options: dict) -> dict:
    result = audit(receipt_from_dict(data))
    writeup = llm.write_up(
        to_brief(result),
        model=options.get("model", llm.DEFAULT_MODEL),
        host=options.get("host", llm.DEFAULT_HOST),
        use_model=options.get("use_model", True),
    )
    return payload(result, writeup)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "Diskwento"
    options: dict = {}

    # -- helpers ----------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page is self-contained; no external origin should be reachable.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("walang ipinadalang data")
        if length > _MAX_BODY:
            raise ValueError("masyadong malaki ang litrato; subukan ang mas maliit")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def log_message(self, fmt_: str, *args) -> None:
        # One tidy line per request; the default logs to stderr very noisily.
        if self.options.get("quiet"):
            return
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    # -- GET --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            try:
                body = (_WEB / "index.html").read_bytes()
            except OSError:
                self._send(500, b"index.html is missing", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return

        if self.path == "/api/health":
            from .ocr import available_backends

            ok, detail = llm.health(self.options.get("host", llm.DEFAULT_HOST))
            self._json(
                200,
                {
                    "ocr_backends": available_backends(),
                    "ollama_ok": ok,
                    "ollama_detail": detail,
                    "model": self.options.get("model", llm.DEFAULT_MODEL),
                    "use_model": self.options.get("use_model", True),
                },
            )
            return

        if self.path == "/api/samples":
            items = []
            for path in sorted(_SAMPLES.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                items.append(
                    {
                        "name": path.name,
                        "merchant": data.get("merchant", path.stem),
                        "note": data.get("_comment", ""),
                    }
                )
            self._json(200, {"samples": items})
            return

        # Static assets out of diskwento/web (an icon, a manifest). Resolved
        # against the real directory and containment-checked, so a crafted
        # path cannot walk out of it.
        name = self.path.lstrip("/").split("?", 1)[0]
        if name and "\\" not in name:
            target = (_WEB / name).resolve()
            try:
                inside = target.is_relative_to(_WEB.resolve())
            except AttributeError:  # Python < 3.9
                inside = str(target).startswith(str(_WEB.resolve()))
            if inside and target.is_file():
                self._send(200, target.read_bytes(), _content_type(target))
                return

        self._json(404, {"error": "hindi mahanap ang pahina"})

    do_HEAD = do_GET

    # -- POST -------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/api/audit":
                self._json(200, self._audit())
            elif self.path == "/api/scan":
                self._json(200, self._scan())
            else:
                self._json(404, {"error": "hindi mahanap ang pahina"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - surfaced in the UI
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _audit(self) -> dict:
        body = self._read_json()

        sample = body.get("sample")
        if sample:
            # Resolve by name only: never let a request reach outside samples/.
            path = _SAMPLES / Path(str(sample)).name
            if not path.is_file():
                raise ValueError(f"walang halimbawang {sample!r}")
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = body.get("receipt")
            if not isinstance(data, dict):
                raise ValueError("kailangan ng 'receipt' o 'sample'")

        return _run_audit(data, self.options)

    def _scan(self) -> dict:
        body = self._read_json()
        raw = body.get("image")
        if not isinstance(raw, str) or not raw:
            raise ValueError("walang litratong ipinadala")

        from .ocr import OcrUnavailable, read_text
        from .parser import parse

        encoded = _DATA_URL.sub("", raw)
        try:
            blob = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError(f"hindi mabasa ang litrato ({exc})") from exc

        suffix = ".png" if blob[:4] == b"\x89PNG" else ".jpg"
        handle, temp = tempfile.mkstemp(suffix=suffix, prefix="diskwento_up_")
        path = Path(temp)
        try:
            with open(handle, "wb") as fh:
                fh.write(blob)
            try:
                text = read_text(path)
            except OcrUnavailable as exc:
                raise ValueError(str(exc)) from exc

            parsed = parse(
                text,
                cardholder=Cardholder(body.get("cardholder", "senior")),
                establishment=body.get("establishment") or None,
                diners_sharing=int(body.get("diners", 1) or 1),
            )
        finally:
            path.unlink(missing_ok=True)

        result = audit(parsed.receipt)
        writeup = llm.write_up(
            to_brief(result),
            model=self.options.get("model", llm.DEFAULT_MODEL),
            host=self.options.get("host", llm.DEFAULT_HOST),
            use_model=self.options.get("use_model", True),
        )
        out = payload(result, writeup)
        out["ocr"] = {
            "establishment": parsed.establishment,
            "needs_confirmation": parsed.needs_confirmation,
            "raw_text": text,
        }
        return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    model: str = llm.DEFAULT_MODEL,
    ollama_host: str = llm.DEFAULT_HOST,
    use_model: bool = True,
    open_browser: bool = True,
) -> None:
    Handler.options = {
        "model": model,
        "host": ollama_host,
        "use_model": use_model,
    }
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"

    print(f"\n  Tama ba ang Diskwento? -- bukas na sa {url}\n")
    if host == "0.0.0.0":
        print(
            "  Nakikita ito ng buong network. Sa telepono, gamitin ang IP ng\n"
            "  computer na ito. Tandaan: ang live camera ay gumagana lamang sa\n"
            "  localhost o HTTPS -- sa telepono gamitin ang 'Pumili ng litrato',\n"
            "  na bubuksan din ang camera.\n"
        )
    print("  Ctrl+C para isara.\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Isinara.\n")
    finally:
        httpd.server_close()
