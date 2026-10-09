"""Image to text, on-device.

Two backends, both local. PaddleOCR is markedly better on thermal receipt
paper; Tesseract is lighter and is usually already installed. Neither is a hard
dependency: import this module on a machine with no OCR at all and you get a
clear error at call time rather than at import time, so the rest of the app
(and the whole JSON-fixture demo path) still runs.

Thermal receipts are a genuinely hard OCR target -- low contrast, curled paper,
dot-matrix glyphs where 8/0/B and 5/S blur together. The preprocessing here
earns more accuracy than swapping models does: upscale, grayscale, autocontrast
and a light sharpen. Hold the camera square to the paper and fill the frame.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

# Small receipts photographed on a phone are often lower resolution than
# Tesseract wants; it does best around 300 DPI equivalent.
_MIN_WIDTH = 1600


class OcrUnavailable(RuntimeError):
    """No local OCR backend is installed."""


def available_backends() -> list[str]:
    backends: list[str] = []
    try:
        import paddleocr  # noqa: F401

        backends.append("paddle")
    except ImportError:
        pass
    if shutil.which("tesseract"):
        backends.append("tesseract")
    return backends


def preprocess(image_path: str | Path) -> Path:
    """Upscale and clean up a receipt photo. Returns a path to a temp PNG.

    Degrades to a no-op (returning the original path) when Pillow is absent,
    because a slightly worse OCR pass beats a crash.
    """
    try:
        from PIL import Image, ImageOps, ImageFilter
    except ImportError:
        return Path(image_path)

    image = Image.open(image_path)
    image = ImageOps.exif_transpose(image).convert("L")
    if image.width < _MIN_WIDTH:
        scale = _MIN_WIDTH / image.width
        image = image.resize(
            (_MIN_WIDTH, int(image.height * scale)), Image.LANCZOS
        )
    image = ImageOps.autocontrast(image, cutoff=2)
    image = image.filter(ImageFilter.UnsharpMask(radius=2, percent=120))

    out = Path(tempfile.mkstemp(suffix=".png", prefix="diskwento_")[1])
    image.save(out)
    return out


def _tesseract(image_path: Path) -> str:
    result = subprocess.run(
        [
            "tesseract",
            str(image_path),
            "stdout",
            "-l",
            "eng",
            # A receipt is a single column of text, not a page of prose.
            "--psm",
            "6",
            # Receipts are digits, money and uppercase item names. Constraining
            # the alphabet cuts the 8/B and 5/S confusions that wreck totals.
            "-c",
            "tessedit_char_blacklist=|{}~^´`",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise OcrUnavailable(f"tesseract failed: {result.stderr.strip()}")
    return result.stdout


def _paddle(image_path: Path) -> str:
    from paddleocr import PaddleOCR

    global _PADDLE
    try:
        engine = _PADDLE
    except NameError:
        engine = _PADDLE = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)

    result = engine.ocr(str(image_path), cls=True)
    lines: list[str] = []
    for page in result or []:
        for entry in page or []:
            # [[box], (text, confidence)]
            lines.append(entry[1][0])
    return "\n".join(lines)


def read_text(image_path: str | Path, backend: str = "auto") -> str:
    """Extract raw text from a receipt photo using a local OCR backend."""
    backends = available_backends()
    if not backends:
        raise OcrUnavailable(
            "No local OCR backend found. Install one:\n"
            "  pip install paddleocr paddlepaddle    (better on receipts)\n"
            "  sudo apt install tesseract-ocr        (lighter)\n"
            "Or skip OCR entirely and audit a JSON receipt:\n"
            "  python -m diskwento audit samples/vat_not_removed.json"
        )
    if backend == "auto":
        backend = backends[0]
    elif backend not in backends:
        raise OcrUnavailable(
            f"backend {backend!r} is not installed; available: {backends}"
        )

    prepared = preprocess(image_path)
    try:
        return _paddle(prepared) if backend == "paddle" else _tesseract(prepared)
    finally:
        if prepared != Path(image_path):
            prepared.unlink(missing_ok=True)
