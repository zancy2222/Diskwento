"""The local language model stage: Ollama, guarded.

Three ideas carry this module.

**The model is a writer, never a calculator.** It receives a brief in which
every number is already a finished string and every legal citation is supplied
verbatim. Its output is prose.

**Everything it writes is verified before the user sees it.** A 1.5B model will
occasionally invent a peso amount or a Republic Act number. :func:`verify`
rejects any output containing a peso amount or RA number that was not in the
brief. This matters more than it sounds: the deliverable is a document the user
waves at a store manager, and one fabricated citation loses the argument
outright.

**The demo cannot depend on the model succeeding.** If Ollama is down, slow, or
returns something unusable twice in a row, :func:`write_up` falls back to a
deterministic template built from the same brief. The letter is plainer, but it
is always correct and it always arrives. Checkout counters have bad signal and
hackathon laptops run out of RAM; the app still works.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .money import CENTAVO

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:3b"
_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_tl.md"

# Peso amounts, as the brief and the model both write them.
_AMOUNT = re.compile(r"₱\s?([\d,]+(?:\.\d{1,2})?)")
# "RA 9994", "R.A. No. 9994", "Republic Act No. 9994".
_RA = re.compile(r"(?:Republic\s+Act|R\.?\s?A\.?)\s*(?:No\.?\s*)?(\d{3,5})")

_REQUIRED_KEYS = ("headline_tl", "summary_tl", "ask_tl", "complaint_tl")


def system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


@dataclass
class Verdict:
    ok: bool
    reasons: list[str]


def _walk(node: object) -> list[str]:
    """Every string anywhere in the brief."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _walk(v)]
    if isinstance(node, (list, tuple)):
        return [s for v in node for s in _walk(v)]
    return []


def _norm(amount: str) -> str:
    """Canonicalize an amount for comparison.

    Via ``Decimal``, not string trimming: stripping trailing zeros would make
    "1,300" and "13" compare equal and quietly open a hole in the guard.
    """
    try:
        return str(Decimal(amount.replace(",", "")).quantize(CENTAVO))
    except InvalidOperation:
        return amount


def allowed_amounts(brief: dict) -> set[str]:
    return {
        _norm(m) for text in _walk(brief) for m in _AMOUNT.findall(text)
    }


def allowed_ra_numbers(brief: dict) -> set[str]:
    return {m for text in _walk(brief) for m in _RA.findall(text)}


def verify(draft: dict, brief: dict) -> Verdict:
    """Reject a draft that invented money or law.

    This is the guard that makes a 1.5B model safe to put in front of a user
    who is about to quote it to a store manager.
    """
    reasons: list[str] = []

    for key in _REQUIRED_KEYS:
        if key not in draft:
            reasons.append(f"missing key {key!r}")
    if reasons:
        return Verdict(False, reasons)

    for key in ("headline_tl", "summary_tl", "ask_tl"):
        if not isinstance(draft[key], str) or not draft[key].strip():
            reasons.append(f"{key} is empty")
    if draft["complaint_tl"] is not None and not isinstance(
        draft["complaint_tl"], str
    ):
        reasons.append("complaint_tl must be a string or null")

    if brief["status"] != "SHORTCHANGED" and draft["complaint_tl"]:
        reasons.append(
            "wrote a complaint letter for a receipt that was not shortchanged"
        )

    text = " ".join(
        v for v in draft.values() if isinstance(v, str)
    )

    permitted = allowed_amounts(brief)
    invented = {_norm(m) for m in _AMOUNT.findall(text)} - permitted
    if invented:
        reasons.append(
            "invented peso amounts not present in the brief: "
            + ", ".join(sorted(invented))
        )

    permitted_ra = allowed_ra_numbers(brief)
    invented_ra = set(_RA.findall(text)) - permitted_ra
    if invented_ra:
        reasons.append(
            "cited Republic Acts not present in the brief: "
            + ", ".join(sorted(invented_ra))
        )

    return Verdict(not reasons, reasons)


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------


def _post(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def health(host: str = DEFAULT_HOST, timeout: float = 3.0) -> tuple[bool, str]:
    """Is Ollama up, and which models does it have? Check this before demoing."""
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=timeout) as r:
            models = [m["name"] for m in json.loads(r.read().decode())["models"]]
        if not models:
            return False, "Ollama is running but has no models pulled"
        return True, "models available: " + ", ".join(models)
    except urllib.error.URLError as exc:
        return False, f"cannot reach Ollama at {host} ({exc.reason})"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"cannot reach Ollama at {host} ({exc})"


def _extract_json(raw: str) -> dict:
    """Pull the JSON object out of a small model's reply.

    Even with ``format: json`` set, a 1.5B model sometimes wraps its object in
    a code fence or adds a sentence of preamble. Take the outermost braces.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the model reply")
    return json.loads(raw[start : end + 1])


def generate(
    brief: dict,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    timeout: float = 120.0,
    nudge: str | None = None,
) -> dict:
    """One guarded round trip to the local model. Raises on any failure."""
    user = "RECEIPT_BRIEF:\n" + json.dumps(brief, ensure_ascii=False, indent=None)
    if nudge:
        user += (
            f"\n\nYour previous reply was rejected: {nudge}\n"
            "Try again, and use only the amounts and laws written in "
            "RECEIPT_BRIEF."
        )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": user},
        ],
        "stream": False,
        # Ollama's JSON mode. Small models need the structural constraint.
        "format": "json",
        "options": {
            # Near-greedy: we want the brief restated faithfully, not creative
            # writing. Temperature is the single biggest lever on whether a
            # small model starts inventing numbers.
            "temperature": 0.2,
            "top_p": 0.9,
            "repeat_penalty": 1.05,
            "num_ctx": 4096,
            "num_predict": 1024,
        },
    }
    data = _post(f"{host}/api/chat", payload, timeout)
    return _extract_json(data["message"]["content"])


# --------------------------------------------------------------------------
# Deterministic fallback
# --------------------------------------------------------------------------


def template(brief: dict) -> dict:
    """Build the write-up from the brief with no model at all.

    Plainer prose than the model produces, but guaranteed correct and
    guaranteed to arrive. This is what ships when Ollama is unreachable.
    """
    a = brief["amounts"]
    who = brief["cardholder"]
    status = brief["status"]

    if status == "CORRECT":
        return {
            "headline_tl": "Tama po ang diskwento sa resibo ninyo.",
            "summary_tl": (
                f"Nasuri po ang resibo at tama ang kuwenta. Ang tamang "
                f"babayaran ay {a['correct_total']}, at iyon din po ang "
                f"nakalimbag sa resibo. Wala pong kulang sa diskwento ninyo."
            ),
            "ask_tl": "Salamat po, tama ang kuwenta ng resibo ko.",
            "complaint_tl": None,
        }

    if status == "OVER_DISCOUNTED":
        return {
            "headline_tl": "Mas malaki po ang naibigay na diskwento sa inyo.",
            "summary_tl": (
                f"Ang tamang babayaran po ay {a['correct_total']} pero "
                f"{a['printed_total']} ang nakalimbag, kaya pabor po sa inyo "
                "ang kuwenta. Wala pong dapat ireklamo."
            ),
            "ask_tl": "Salamat po, tama o pabor sa akin ang kuwenta.",
            "complaint_tl": None,
        }

    if status == "CANNOT_VERIFY":
        return {
            "headline_tl": "Hindi po mabasa ang total sa resibo.",
            "summary_tl": (
                f"Hindi po nabasa ng app ang total, kaya hindi masabi kung "
                f"tama ang diskwento. Base po sa mga item, ang tamang "
                f"babayaran ay {a['correct_total']}. Mainam pong ikuha ng mas "
                "malinaw na litrato ng resibo."
            ),
            "ask_tl": (
                "Pasensya na po, pwede po bang ipatingin muli ang kuwenta ng "
                "resibo ko?"
            ),
            "complaint_tl": None,
        }

    # SHORTCHANGED.
    steps = "\n".join(f"  {s}" for s in brief["computation_steps"])
    legal = "\n\n".join(c["text_tl"] for c in brief["citations"])
    merchant = brief["merchant"]
    date = brief.get("date") or "(petsa sa resibo)"
    number = brief.get("receipt_no") or "(blg. sa resibo)"
    id_word = "senior citizen" if who == "senior citizen" else "person with disability"

    letter = (
        f"Sa Butihing Manager ng {merchant},\n\n"
        f"Magandang araw po. Ako po ay {id_word} at ipinakita ko ang aking ID "
        f"sa transaksyon noong {date}, Resibo Blg. {number}.\n\n"
        # Not .lower() on the finding: it would turn "VAT" into "vat".
        f"Sa muling pagsusuri po ng resibo: {brief['finding_tl']} "
        f"{brief['explanation_tl']}\n\n"
        f"Ganito po ang tamang pagkuwenta:\n\n{steps}\n\n"
        f"Ang kabuuang dapat bayaran po ay {a['correct_total']}, ngunit "
        f"{a['printed_total']} ang nakalimbag sa resibo, kaya may sobra pong "
        f"{a['overcharge']}.\n\n"
        f"{legal}\n\n"
        f"Hindi po ako naghahanap ng away. Nais ko lamang pong hilingin na "
        f"muling suriin ang kuwenta at maibalik ang {a['overcharge']} kung "
        f"tama po ang aking napansin. Maraming salamat po sa inyong panahon "
        f"at pag-unawa.\n\n"
        f"Lubos na gumagalang,\n\n"
        f"________________________\n"
        f"Pangalan / OSCA o PWD ID Blg."
    )

    return {
        "headline_tl": f"Kulang po ang diskwento ninyo ng {a['overcharge']}.",
        "summary_tl": (
            f"{brief['finding_tl']} Dapat po ay {a['correct_total']} na lamang "
            f"ang babayaran ninyo, pero {a['printed_total']} ang nakalimbag sa "
            f"resibo. May sobra po kayong {a['overcharge']}."
        ),
        "ask_tl": (
            "Pasensya na po, pwede po bang ipatingin muli ang kuwenta ng "
            "diskwento ko? Mukhang may kulang po."
        ),
        "complaint_tl": letter,
    }


# --------------------------------------------------------------------------
# What callers use
# --------------------------------------------------------------------------


@dataclass
class WriteUp:
    headline_tl: str
    summary_tl: str
    ask_tl: str
    complaint_tl: str | None
    source: str  # "model" or "template"
    notes: list[str]


def write_up(
    brief: dict,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    use_model: bool = True,
    timeout: float = 120.0,
) -> WriteUp:
    """Turn a brief into user-facing Filipino. Never raises.

    Tries the local model, verifies the result, retries once with the
    rejection reason, then falls back to the deterministic template.
    """
    notes: list[str] = []
    if not use_model:
        out = template(brief)
        return WriteUp(**out, source="template", notes=["model skipped"])

    nudge: str | None = None
    for attempt in (1, 2):
        try:
            draft = generate(
                brief, model=model, host=host, timeout=timeout, nudge=nudge
            )
        except Exception as exc:
            notes.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            break

        verdict = verify(draft, brief)
        if verdict.ok:
            return WriteUp(
                headline_tl=draft["headline_tl"],
                summary_tl=draft["summary_tl"],
                ask_tl=draft["ask_tl"],
                complaint_tl=draft["complaint_tl"],
                source="model",
                notes=notes,
            )
        nudge = "; ".join(verdict.reasons)
        notes.append(f"attempt {attempt} rejected: {nudge}")

    notes.append("fell back to the deterministic template")
    out = template(brief)
    return WriteUp(**out, source="template", notes=notes)
