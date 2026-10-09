"""Tama ba ang Diskwento? -- PWD & Senior Citizen discount auditor.

Local-first by design: the arithmetic is pure Python, OCR runs on-device, and
the language model is a local Ollama instance. Nothing about a receipt or an ID
leaves the handset.
"""

from .engine import Receipt, LineItem, audit, to_brief
from .law import Cardholder, Category

__version__ = "0.1.0"
__all__ = ["Receipt", "LineItem", "audit", "to_brief", "Cardholder", "Category"]
