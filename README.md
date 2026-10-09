# Tama ba ang Diskwento?

**A PWD & Senior Citizen discount auditor that runs entirely on your phone.**

Snap a receipt. The app recomputes the 20% discount and 12% VAT exemption the
way RA 9994 and RA 10754 require, tells you in plain Filipino whether you were
shortchanged, and — if you were — drafts a polite, law-citing letter you can
show the manager before you leave the counter.

AppBuildersPH Hackathon 2026 · Local AI

---

## The problem, in one line of arithmetic

A cashier who takes "20% off" at face value is **undercharging the discount
every single time**.

```
PHP 112.00 of covered goods

  WRONG:  112.00 × 0.80                       = PHP 89.60
  RIGHT:  112.00 ÷ 1.12 = 100.00, × 0.80      = PHP 80.00
                                                ---------
                                   shortchanged by PHP 9.60
```

The 12% VAT must be removed **first**, and the 20% is computed on what remains.
The real reduction off the shelf price is **28.57%**, not 20%. That gap is
every receipt, every day, nationwide — and almost nobody checks, because doing
it in your head at the counter is genuinely hard.

## Why local AI, not cloud

- **Signal.** The moment of use is at the checkout counter with the queue
  waiting. That is a basement supermarket with one bar. A cloud call that
  fails is a feature that does not exist.
- **Privacy.** The inputs are a purchase history and an OSCA or PWD ID number —
  for a PWD card, the disability itself. Sensitive personal information under
  the Data Privacy Act. Not transmitting it beats transmitting it carefully.

Nothing here opens a socket except to `localhost:11434`.

## Quickstart

Everything below — the engine, the letter templates, the Ollama client **and
the web GUI** — is pure standard library. Clone and run; no `pip install`:

```bash
python -m diskwento serve                   # the GUI, opens in your browser
python -m unittest discover -s tests -t .   # 67 tests, zero dependencies
python -m diskwento demo --no-llm           # the whole pitch, six receipts
python -m diskwento doctor                  # what is installed and ready
```

On Windows, double-click **`run-gui.bat`** for the GUI or **`run-demo.bat`**
for the terminal version.

### The GUI

`python -m diskwento serve` starts a local server on `127.0.0.1:8765` and
opens it. Three ways in:

- **Litrato** — capture with the camera, or pick a photo. Needs OCR installed.
- **Manu-mano** — type the line items. Works with nothing installed at all.
- **Halimbawa** — audit the six bundled receipts. The fastest way to demo.

The result shows the verdict, the full arithmetic trail, a sentence to say to
the cashier, and the complaint letter with copy and print buttons.

A browser rather than a desktop toolkit because the browser already has a
camera: no OpenCV wheels to fight on Windows, and the same page opens on a
phone. `--lan` binds all interfaces so a phone on the same wifi can reach it
(`run-gui-lan.bat`). Note that the live camera needs a secure context, so over
plain HTTP on a LAN address use "Pumili ng litrato" — on a phone that opens
the camera app anyway.

On Windows use `py` in place of `python`. The CLI forces UTF-8 output and
enables ANSI colour itself, so the peso sign prints correctly in `cmd`,
PowerShell and Windows Terminal without any setup.

Add the local model for fluent letters:

```bash
ollama serve
ollama pull qwen2.5:3b        # or qwen2.5:1.5b on a tight laptop
python -m diskwento demo
```

Add OCR for photos:

```bash
pip install paddleocr paddlepaddle pillow     # better on receipt paper
# or: sudo apt install tesseract-ocr && pip install pillow

python -m diskwento scan receipt.jpg --cardholder senior --diners 4
```

## What it actually handles

Beyond the headline calculation, the cases that separate an auditor from a
calculator:

- **Shared restaurant bills** — the privilege covers the cardholder's share,
  not the table's.
- **Basic necessities** — a *separate* 5% discount, no VAT exemption, capped
  at PHP 1,300 per calendar week.
- **Non-VAT-registered sellers** — no VAT to strip, so 20% comes straight off.
- **Excluded items** — alcohol, tobacco, ordinary retail merchandise.
- **No double discount** — statutory vs. promo, whichever is better for the
  cardholder.
- **Service charges** — outside the discount base.
- **Correct receipts** — reported as correct. A false accusation sends a
  senior to argue with a manager who is right, so this matters as much as
  catching the errors.

And rather than just reporting a gap, it **names the mistake**: "the VAT was
never removed before the discount" is an argument; "your total is wrong" is
not.

## Architecture

```
 [1] OCR  ---->  [2] PARSE  ---->  [3] ENGINE  ---->  [4] LLM
  on-device       regex +           Decimal            Ollama
                  classifier        arithmetic         Qwen 2.5
  unreliable      unreliable        AUTHORITATIVE      unreliable
  (flag it)       (confirm it)      (test it)          (verify it)
```

Stage 3 is the only stage allowed to produce a number, and the only one with
no model in it. The LLM receives a brief where every amount is already a
finished string and every citation is supplied verbatim — then its output is
**verified** against that brief and rejected if it invented a peso amount or a
Republic Act number. If Ollama is unreachable, a deterministic template takes
over. The demo cannot die on stage.

Full detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

| Module | Role |
|---|---|
| `engine.py` | The deterministic math. Pure, tested, authoritative. |
| `law.py` | Rates, eligible categories, citation strings. One source of legal truth. |
| `llm.py` | Ollama client, hallucination guard, template fallback. |
| `ocr.py` | Pluggable on-device OCR with receipt preprocessing. |
| `parser.py` | OCR text to structured receipt, with confirmation flags. |
| `server.py` | Local web GUI on `http.server`. No framework, no CDN. |
| `web/index.html` | The whole front end: one self-contained file. |
| `prompts/system_tl.md` | The model's system prompt. |

## Before you demo

```bash
python -m diskwento.law      # lists citations still to verify
```

Citations carry a `needs_review` flag. The ones marked pending are written
from the substance of the law but their exact section numbers have **not** been
checked against the official text — do that against the Official Gazette, BIR
issuances, and the DSWD / NCDA implementing rules. A judge who knows the law
will ask, and flagging what you have not verified is a much better answer than
a confidently wrong section number.

## Privacy

Nothing leaves the machine. The GUI binds `127.0.0.1` by default, the page
loads no external scripts, fonts or stylesheets, and photos are held in a
temp file only for the duration of the OCR pass and deleted straight after.
The one outbound connection anything makes is to Ollama on `localhost:11434`.

## Scope, honestly

This audits **arithmetic**. It does not decide whether an ID is valid, whether
an establishment is covered, or whether a purchase was for the cardholder's
exclusive use. Those are judgment calls that belong to a person. It is a tool
for asking a better question at the counter, not legal advice.
