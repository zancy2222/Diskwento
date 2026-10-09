# System Architecture & Workflow

## The governing idea

The pipeline has four stages, and they are ordered by how much we trust them.

```
   photo            text             numbers           words
     |                |                 |                |
 [1] OCR  ----->  [2] PARSE  ----->  [3] ENGINE  ---->  [4] LLM
  on-device        regex +            Decimal           Ollama
  Paddle /         keyword            arithmetic        Qwen 2.5
  Tesseract        classifier         (pure Python)     1.5B / 3B
     |                |                 |                |
  unreliable      unreliable         AUTHORITATIVE     unreliable
  (flag it)       (confirm it)       (test it)         (verify it)
```

Stage 3 is the only stage allowed to produce a number, and it is the only
stage with no model in it. Stages 1, 2 and 4 are all probabilistic, so each
one is wrapped in something that catches it being wrong: OCR output gets
cross-checked against the receipt's own subtotal, parser guesses get surfaced
for user confirmation, and model output gets verified against the engine's
brief before it reaches the screen.

That ordering is the whole design. A discount auditor that is occasionally
wrong about money is worse than no auditor, because it sends a senior citizen
to argue with a manager who turns out to be right. So the arithmetic is
deterministic and unit-tested, and the language model is confined to the one
job it is actually good at: writing fluent, polite Filipino.

## Why this has to be local

Two independent reasons, and either alone would be sufficient:

**Signal.** The moment of use is at the counter, with the queue waiting and
the receipt still in hand. That is a basement supermarket with one bar of
signal. A cloud round trip that fails is a feature that does not exist.

**Privacy.** The inputs are a purchase history and an OSCA or PWD ID number —
and for a PWD card, the disability itself. That is sensitive personal
information under the Data Privacy Act. Not transmitting it is categorically
safer than transmitting it carefully. Nothing in this pipeline opens a socket
except to `localhost:11434`.

## Stage by stage

### 1. Capture and OCR — `diskwento/ocr.py`

```
image -> preprocess -> local OCR -> raw text
```

Preprocessing earns more accuracy here than changing models does. Thermal
receipt paper is low-contrast, curls, and uses dot-matrix glyphs where `8`,
`0` and `B` blur together. So: EXIF-rotate, grayscale, upscale to ~1600px
wide (phone photos of small receipts are below what Tesseract wants),
autocontrast, unsharp mask.

Backends are pluggable and neither is a hard dependency:

| Backend | Use when |
|---|---|
| PaddleOCR | Default. Noticeably better on receipt paper. |
| Tesseract | Lighter, usually already installed. `--psm 6` for single-column. |

If no backend is installed the error message names the install command and
the JSON path, so the demo still runs.

### 2. Parse and classify — `diskwento/parser.py`

```
raw text -> {merchant, line items, printed totals, VAT status} + confirmations
```

- **Field extraction.** Labelled-line regexes for `VAT EXEMPT SALE`,
  `VATABLE SALE`, `SC/PWD DISC`, `SERVICE CHARGE`, `AMOUNT DUE`. Longest
  label matches first so `VAT EXEMPT SALE` is not swallowed by `VAT`.
- **Digit repair.** `O→0`, `l→1`, `S→5`, `B→8`, applied *inside numbers only* —
  running it over item names would mangle them.
- **Establishment inference.** The header's trade name decides the
  establishment type, and that is what resolves genuinely ambiguous items.
  "RICE" is a restaurant item at a carinderia (20% + VAT exemption) and a
  basic necessity at a grocery (5%, no VAT exemption). Guessing wrong costs
  15 percentage points, so context decides rather than a coin flip.
- **Fail-safe default.** An unrecognised item defaults to *no discount*. A
  parsing failure therefore under-claims rather than over-claims.
- **Cross-check.** Line items are summed against the receipt's printed
  subtotal. A mismatch over PHP 1.00 means a line was missed, and that is
  reported rather than silently audited.

Everything uncertain lands in `needs_confirmation` for the UI to put in front
of the user *before* any letter is written.

### 3. The rule engine — `diskwento/engine.py`

```
Receipt -> per-line treatment -> expected totals -> fingerprint -> Audit
```

Pure `Decimal` arithmetic, no I/O, no model, no randomness. Three treatments:

| Treatment | Computation |
|---|---|
| 20% + VAT exemption | `gross / 1.12`, then `× 0.80` |
| 5% basic necessities | `× 0.95`, VAT stays, PHP 1,300/week ceiling |
| No privilege | unchanged |

It also handles the cases that separate a real auditor from a calculator:
shared restaurant bills (the privilege covers the cardholder's share only),
non-VAT-registered sellers (no VAT to strip), the no-double-discount rule
(statutory vs. promo, whichever is better for the cardholder), excluded items,
and service charges outside the discount base.

Then the part that makes a complaint persuasive — **fingerprinting**. Rather
than only reporting a gap, the engine replays the ways a POS gets this wrong
and reports which one reproduces the printed total:

| Pattern | What the merchant did |
|---|---|
| `VAT_NOT_EXEMPTED` | 20% off the VAT-inclusive price, VAT left on |
| `VAT_EXEMPT_ONLY` | stripped the VAT, forgot the 20% |
| `NO_PRIVILEGE_APPLIED` | charged full price |
| `WRONG_RATE_ON_BASE:r` | right base, wrong rate |
| `WRONG_RATE_ON_GROSS:r` | wrong base *and* wrong rate |
| `BASIC_NECESSITY_DISCOUNT_MISSING` | covered lines right, ignored the 5% |
| `VAT_DEDUCTED_TWICE` | over-discounted; favours the customer |
| `UNRECOGNIZED` | gap is real but unexplained — say exactly that |

Naming the mistake is what turns "your total is wrong" into "the VAT was never
removed before the discount". `UNRECOGNIZED` exists so the engine can admit
ignorance instead of handing the model a story to dress up.

Tolerance is `max(PHP 0.05, PHP 0.01 × lines)`, so a POS that rounds
differently is not called a thief.

### 4. The local LLM — `diskwento/llm.py`

```
Audit -> brief (pre-formatted) -> Ollama -> verify -> WriteUp
                                     |         |
                                     +-- fail --+-> deterministic template
```

The model receives `to_brief()`: a compact JSON object in which **every number
is already a finished string** (`"₱80.00"`) and **every legal citation is
supplied verbatim**. It is a writer, not an auditor.

Three guards make a 1.5B model safe to put in front of a user who is about to
quote it to a store manager:

1. **No arithmetic reaches it.** Pre-formatted strings, because a small model
   asked to format currency drops a centavo or moves a comma.
2. **Output is verified, not trusted.** `verify()` rejects any draft
   containing a peso amount or a Republic Act number that was not in the
   brief. Models invent plausible-looking RA numbers, and one fabricated
   citation loses the argument outright. Rejection triggers one retry carrying
   the reason.
3. **The demo cannot depend on it.** Ollama down, slow, or unusable twice →
   fall back to a deterministic template built from the same brief. Plainer
   prose, always correct, always arrives.

Generation settings: `format: json`, `temperature 0.2`, `num_ctx 4096`.
Temperature is the single biggest lever on whether a small model starts
inventing numbers.

Output is one JSON object: `headline_tl`, `summary_tl`, `ask_tl` (a sentence
the user can say out loud at the counter), and `complaint_tl` (the letter, or
`null` when nothing is owed).

## Trust boundary, stated once

```
  TRUSTED                          |  UNTRUSTED (verified at the boundary)
  ---------------------------------+--------------------------------------
  engine.py   pure Decimal math    |  ocr.py      misreads digits
  law.py      citation strings     |  parser.py   misclassifies items
  (unit-tested, deterministic)     |  llm.py      invents numbers and laws
```

Citations live in exactly one place, `law.py`, and carry a `needs_review`
flag. `python -m diskwento.law` prints the ones still to be checked against
the official text. Run it before demoing — a judge who knows the law will
ask, and "we flagged exactly what we had not verified" is a much better
answer than a confidently wrong section number.

## Degradation

| Missing | Result |
|---|---|
| Ollama | Template letters. Full audit. |
| OCR | JSON and manual entry. Full audit. |
| Both | `python -m diskwento demo` still runs the whole pitch. |
| Network | Irrelevant — nothing but `localhost` is ever contacted. |

Check readiness with `python -m diskwento doctor`.
