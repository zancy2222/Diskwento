"""Command line entry point -- and the demo script.

    python -m diskwento doctor                      # is the machine ready?
    python -m diskwento audit samples/*.json        # audit a JSON receipt
    python -m diskwento scan receipt.jpg            # audit a photo
    python -m diskwento demo                        # run every sample

``--no-llm`` everywhere skips Ollama and uses the deterministic template, which
is the safe setting if you are demoing on a laptop under load.
"""

from __future__ import annotations

import argparse
import glob
import sys
from decimal import Decimal

from . import law, llm
from .engine import CORRECT, SHORTCHANGED, audit, to_brief
from .law import Treatment
from .loader import receipt_from_json
from .money import ZERO, fmt

_RULE = "─" * 64

_TREATMENT_LABEL = {
    Treatment.TWENTY_PCT_VAT_EXEMPT: "bawas 20% (VAT-exempt)",
    Treatment.FIVE_PCT_CAPPED: "bawas 5% (basic necessity)",
    Treatment.NO_PRIVILEGE: "bawas promo",
}


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def _report(result, writeup: llm.WriteUp) -> None:
    r = result.receipt
    print(_RULE)
    header = r.merchant or "Resibo"
    if r.date or r.receipt_no:
        header += f"   {r.date} {r.receipt_no}".rstrip()
    print(_c(header, "1"))
    print(_RULE)

    for line in result.lines:
        print(f"  {line.description[:38]:<38} {fmt(line.gross):>12}")
        if line.discount > ZERO:
            print(
                f"    {_TREATMENT_LABEL[line.treatment]:<36} "
                f"{_c('-' + fmt(line.discount), '36'):>12}"
            )
    if r.service_charge > ZERO:
        print(f"  {'Service charge':<38} {fmt(r.service_charge):>12}")

    print(_RULE)
    for step in result.steps:
        print(f"  {step}")
    print(_RULE)
    print(f"  {'DAPAT BAYARAN':<38} {_c(fmt(result.expected_total), '1;32'):>12}")
    if r.printed_total is not None:
        print(f"  {'NAKALIMBAG SA RESIBO':<38} {fmt(r.printed_total):>12}")

    if result.status == SHORTCHANGED:
        banner = _c(f" KULANG NG {fmt(result.overcharge)} ", "1;37;41")
    elif result.status == CORRECT:
        banner = _c(" TAMA ANG DISKWENTO ", "1;37;42")
    else:
        banner = _c(f" {result.status} ", "1;30;43")
    print(f"\n  {banner}  {writeup.headline_tl}\n")
    print(f"  {writeup.summary_tl}\n")
    print(f"  Masasabi ninyo: “{writeup.ask_tl}”\n")

    for warning in result.warnings:
        print(_c(f"  [paalala] {warning}", "33"))
    if result.warnings:
        print()

    if writeup.complaint_tl:
        print(_c("  SULAT NA MAIPAPAKITA SA MANAGER", "1"))
        print(_RULE)
        for line in writeup.complaint_tl.splitlines():
            print(f"  {line}")
        print(_RULE)

    print(
        _c(
            f"  [sulat galing sa: {writeup.source}]"
            + ("  " + "; ".join(writeup.notes) if writeup.notes else ""),
            "90",
        )
    )
    print()


def _run(path: str, args) -> str:
    result = audit(receipt_from_json(path))
    writeup = llm.write_up(
        to_brief(result),
        model=args.model,
        host=args.host,
        use_model=not args.no_llm,
    )
    _report(result, writeup)
    return result.status


def _doctor(args) -> int:
    print(_c("Kalagayan ng makina\n", "1"))

    from .ocr import available_backends

    backends = available_backends()
    print(f"  OCR backends : {', '.join(backends) if backends else _c('WALA', '31')}")

    ok, detail = llm.health(args.host)
    print(f"  Ollama       : {_c('OK', '32') if ok else _c('DOWN', '31')}  {detail}")

    pending = law.unverified()
    verified = len(law.CITATIONS) - len(pending)
    print(f"  Citations    : {verified}/{len(law.CITATIONS)} verified")
    if pending:
        print(
            _c(
                "                 to verify: "
                + ", ".join(c.key for c in pending),
                "33",
            )
        )

    print(
        "\n  Ang deterministic engine ay tumatakbo kahit walang OCR at walang\n"
        "  Ollama. Ang sulat ay may template na fallback.\n"
    )
    if not backends:
        print("  Para sa OCR:  pip install paddleocr paddlepaddle")
    if not ok:
        print(f"  Para sa LLM:  ollama serve && ollama pull {args.model}")
    if pending:
        print("  Para sa batas: python -m diskwento.law")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="diskwento",
        description="Tama ba ang Diskwento? -- PWD & Senior Citizen discount auditor",
    )
    # Shared flags live on a parent parser so they work after the subcommand
    # ("demo --no-llm"), which is where anyone demoing will naturally type them.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default=llm.DEFAULT_MODEL)
    common.add_argument("--host", default=llm.DEFAULT_HOST)
    common.add_argument(
        "--no-llm",
        action="store_true",
        help="skip Ollama and use the deterministic letter template",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser(
        "audit", parents=[common], help="audit one or more JSON receipts"
    )
    p_audit.add_argument("paths", nargs="+")

    p_scan = sub.add_parser("scan", parents=[common], help="audit a receipt photo")
    p_scan.add_argument("image")
    p_scan.add_argument("--backend", default="auto", choices=["auto", "paddle",
                                                              "tesseract"])
    p_scan.add_argument("--cardholder", default="senior", choices=["senior", "pwd"])
    p_scan.add_argument("--establishment", default=None)
    p_scan.add_argument("--diners", type=int, default=1,
                        help="how many people shared the bill")
    p_scan.add_argument("--show-text", action="store_true")

    sub.add_parser("demo", parents=[common], help="audit every receipt in samples/")
    sub.add_parser(
        "doctor", parents=[common], help="check OCR, Ollama and citation readiness"
    )

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor(args)

    if args.command == "demo":
        paths = sorted(glob.glob("samples/*.json"))
        if not paths:
            print("No samples/*.json found; run from the repository root.")
            return 1
        shortchanged = 0
        for path in paths:
            print(_c(f"\n### {path}", "1;36"))
            if _run(path, args) == SHORTCHANGED:
                shortchanged += 1
        print(
            _c(
                f"{len(paths)} resibo ang sinuri, {shortchanged} ang kulang "
                "sa diskwento.\n",
                "1",
            )
        )
        return 0

    if args.command == "audit":
        for path in args.paths:
            _run(path, args)
        return 0

    # scan
    from .ocr import OcrUnavailable, read_text
    from .parser import parse

    try:
        text = read_text(args.image, backend=args.backend)
    except OcrUnavailable as exc:
        print(_c(str(exc), "31"))
        return 2

    if args.show_text:
        print(_c("--- OCR ---", "90"))
        print(text)
        print(_c("-----------", "90"))

    try:
        parsed = parse(
            text,
            cardholder=law.Cardholder(args.cardholder),
            establishment=args.establishment,
            diners_sharing=args.diners,
        )
    except ValueError as exc:
        print(_c(str(exc), "31"))
        return 2

    print(_c(f"[uri ng establisimyento: {parsed.establishment}]", "90"))
    for note in parsed.needs_confirmation:
        print(_c(f"  [kailangang kumpirmahin] {note}", "33"))
    if parsed.needs_confirmation:
        print(
            _c(
                "  Tingnan po ang mga nasa itaas bago gamitin ang sulat.\n",
                "33",
            )
        )

    result = audit(parsed.receipt)
    writeup = llm.write_up(
        to_brief(result),
        model=args.model,
        host=args.host,
        use_model=not args.no_llm,
    )
    _report(result, writeup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
