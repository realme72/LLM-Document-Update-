# LLM-Driven Document Updates

Takes `{ sourceDocument, changes[] }` (or a PDF + changes) and returns the updated document plus a
per-change report. Python, OpenAI `gpt-4.1-mini`, CLI + FastAPI.

## How to run and test

### 1. Setup (Python 3.10+)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env               # then put your key in .env: OPENAI_API_KEY=sk-...
.venv/bin/python tests/make_sample_doc.py   # (re)builds samples/nimbus_guide.pdf, ~10.5k words
```

### 2. Run with the spec input shape `{ sourceDocument, changes }`

`sourceDocument` is the **full text** of the document (Markdown or plain text), not a file name.
Ready-made examples:

```bash
.venv/bin/python -m docupdate.cli --input examples/input.json -o out/tiny            # tiny doc, 3 changes
.venv/bin/python -m docupdate.cli --input examples/invoice_input.json -o out/invoice # 1-page invoice, 10 changes
```

To make your own input from a PDF, extract the text into the right shape, edit `"changes"` in the
generated file, then run it:

```bash
.venv/bin/python tests/pdf_to_input.py samples/invoice-payment-agreement-template.pdf examples/my_input.json
.venv/bin/python -m docupdate.cli --input examples/my_input.json -o out/mine
```

### 3. Run directly on a file

```bash
# changes inline (repeat -c); use single quotes or \$ for dollar signs
.venv/bin/python -m docupdate.cli --pdf samples/nimbus_guide.pdf \
  -c 'Update the pricing section to reflect a 10% increase across all tiers' \
  -c 'Remove the paragraph about the legacy API' \
  -c 'Make the tone throughout more formal' -o out/run1

# or changes from a JSON list file: ["change one", "change two"]
.venv/bin/python -m docupdate.cli --pdf samples/invoice-payment-agreement-template.pdf \
  --changes-file my_changes.json -o out/invoice

# --md <file> for Markdown/text input · --model gpt-5.4-mini to switch models · --no-verify to skip the reviewer
# env: DOCUPDATE_CONCURRENCY (calls in flight per document, default 32)
#      DOCUPDATE_GLOBAL_CONCURRENCY (calls in flight across all documents in the process, default 64)
#      DOCUPDATE_STYLE_MODEL (model for style-only chunks; default = main model)
#      DOCUPDATE_PLANNER=outline (compressed planner input; default full - see Optimizations)
```

Output folder: `updated.md`, `updated.pdf` (when the input was a PDF), `report.json` (per-change
status, retries, tokens and latency per call) and `source.md` (the extracted original, for diffing).
The terminal prints a one-line summary plus the status of each change: `applied`, `partial`,
`failed` or `not_found`.

### 4. API (FastAPI) and browser UI

```bash
.venv/bin/uvicorn docupdate.api:app --port 8001 --reload
```

Open **http://127.0.0.1:8001/docs**, pick an endpoint, then click **Try it out** and **Execute**.

- `POST /update`: JSON body `{"sourceDocument": "...", "changes": ["..."], "verify": true}`
- `POST /update/pdf`: upload `file` (a PDF). `changes` is a text field: paste a JSON list such as
  `["Replace [State] with California.", "Set the date to October 23, 2026."]`. Curly quotes from
  copy-paste are accepted; so is one change per line.

```bash
curl -X POST localhost:8001/update/pdf -F file=@samples/invoice-payment-agreement-template.pdf \
  -F 'changes=["Replace [State] in the governing law clause with California."]'
```

If you get `address already in use`, another app holds the port: pick another with `--port`.

### 5. Tests

```bash
.venv/bin/python tests/run_tests.py            # 9 offline fault-injection tests + 4 live tests (~1 min, OpenAI calls)
.venv/bin/python tests/run_tests.py --offline  # offline only: scripted fake LLM, no API key or cost
```

Each test prints PASS/FAIL with time, number of calls and tokens. Results are written to
`out/tests/results.json`. A saved copy from our run is in `examples/test_results.json`.

## Architecture

```
PDF ──pymupdf4llm──▶ Markdown ──split──▶ blocks [B1..Bn] (id, kind, section path)
                                            │
   1. PLAN       1 call, whole doc in, tiny JSON out: change -> block ids | "global" | "not_found"
   2. PRE-SWEEP  code: other blocks stating the same prices/placeholders join the change (optional)
   3. EDIT       parallel calls, each sees ONLY its blocks, returns {id, replace|delete|keep, text}
                 (small docs: one call for all targeted changes)
   4. CHECK      deterministic validators per block
   5. VERIFY     one LLM review per job of the touched blocks' before/after; skipped when code
                 already proved the change (exact percentage scaling)
   6. RETRY      only the failed blocks, with the validator errors as feedback (max 2 retries)
   7. SWEEP      safety net: values/terms a change replaced ("$79", "Pro") still present elsewhere
                 -> one follow-up edit call
   8. APPLY      code splices accepted edits back in; drops headings whose whole section was deleted
```

**Splitting (no LLM).** The PDF is converted to Markdown with PyMuPDF4LLM, which infers headings
from font size and emits pipe tables. Repeated running headers and footers and page numbers are
removed: these are lines at the page edge that repeat on at least half the pages. A paragraph
broken by a page break is re-joined (the previous page ends mid-sentence and the next page starts
lower-case). The Markdown is then split on blank lines into blocks. Headings, tables, code fences
and lists each stay one block. Every block gets an ID and its breadcrumb, e.g.
`B77 [Pricing]`.

**Matching.** The planner gets the document with IDs and only *points*. It never rewrites text.
The model is told to be exhaustive: facts restated in the FAQ or Summary must be included.
Two deterministic safety nets cover planner misses:
- **Heading repair.** If the planner picks a heading, the block after it is added too. We saw
  `gpt-4.1-mini` pick "## Summary" when it meant the summary paragraph.
- **Pre-sweep (before editing).** Code collects the prices, percentages and `[placeholders]` in
  the planner's blocks and finds every other block stating them. Those blocks join the change as
  *optional* blocks in the first edit round, listed separately in the prompt ("also check: these
  mention the same values"). If an optional block keeps failing, it stays unchanged and does not
  roll back the change. This catches the FAQ's `$79` without a second round.
- **Sweep (after editing, safety net).** Code diffs before/after values plus terms the change names
  (`'Pro'` disappearing), then greps the rest of the document. Remaining hits get one follow-up
  edit call, which may answer `keep` (e.g. "Pro tip"). Plain names are left to this step on
  purpose: they are ambiguous, so pre-sweeping 'Pro' pulled 20 "Pro tip" blocks into round 1 and
  cost about 30% more tokens. In testing this caught "everything in Pro", which the planner missed.

**Why this shape — token and latency scaling.** Output tokens are the expensive, slow part
(roughly 50–100 tok/s). A naive full rewrite has output O(doc): about 14k tokens and minutes per
10k-word document, every time, even to change one sentence. Here:

| | input tokens | output tokens | wall time |
|---|---|---|---|
| Planner (always) | ≈ 1× doc (~13k) | O(#changes), ~30–300 | 1–3 s |
| Targeted edits | O(touched blocks) | O(edit size) | parallel, ~2–5 s |
| Global style change | ≈ 1× doc, chunked | ≈ 1× doc (inherent: every sentence changes) | **one chunk's** latency: chunks of ~700 words run in parallel |

Measured on the 10.5k-word sample (`gpt-4.1-mini`):

| run | calls | in | out | time | first version |
|---|---|---|---|---|---|
| pricing +10% + remove legacy paragraph | 4 | 14.9k | **0.48k** | 4.5 s | 5 calls, 5.8 s |
| rename "Pro" → "Growth" everywhere | 5 | 22.6k | 0.96k | 8.8 s | 5 calls, ~33 s |
| spec example (pricing + legacy + formal tone) | 29 | 40.2k | 13.5k | **10.0 s** | 32 calls, 29.5 s |
| change that matches nothing | 1 | 13.3k | 29 | 1.0 s | same |
| 1-page invoice, 10 targeted changes | 3 | 3.7k | 0.82k | 6.3 s | 17 calls, 7.3k in |

"First version" is before the optimization pass described below. Each figure is a single run, so
latency varies by a few seconds between runs.

Targeted changes cost about 1× the document as input and almost nothing as output. Only a
whole-document style change pays O(doc) output. That cost is unavoidable, but it is parallelized,
so latency stays flat as the document grows. Unchanged blocks in a style pass can answer `keep`
instead of repeating their text. Retries re-send only the failing blocks, not the chunk or the
document.

**Concurrency.** There are two limits. Each document may have up to 32 LLM calls in flight
(`DOCUPDATE_CONCURRENCY`). That is above the 17 chunks a 10k-word style pass needs, so the
whole pass runs in one wave and its latency is the slowest single chunk. The whole process
(API server or batch) is capped at 64 calls in flight (`DOCUPDATE_GLOBAL_CONCURRENCY`), so many
simultaneous documents queue instead of bursting at the provider. Raising the per-document cap
from 8 to 32 took the spec example from 22.2s to 13.4s with the same tokens; each figure is a
single run. For scale: at 64 calls in flight and about 5s per call, one process does about 750
calls/min. That is roughly 23 spec-example-sized documents per minute (32 calls each), or about 150
targeted-only documents per minute (5 calls each). This key's OpenAI limits (30k requests/min,
150M tokens/min) are far above that, so the global cap is the dial to turn.

**Model choice.** `gpt-4.1-mini` is a non-reasoning model, so there are no hidden reasoning tokens
and latency is predictable. It supports strict JSON-schema outputs and long context, and it is
cheap. Override with `DOCUPDATE_MODEL` or `--model`; gpt-5 models are called with reasoning set to
none or minimal. Cheaper models for the style chunks were measured and rejected (see Optimizations). Arithmetic is not left to the model at all:
for "N% increase/decrease" changes, code computes the new amounts and passes them in as a lookup
table. See the "switches" section below for why.

## Reliability approach

"Shouldn't silently apply a bad change" + "shouldn't hard-fail on the first bad output":

1. **Strict JSON schema** on every call (OpenAI structured outputs), then `json.loads` anyway.
   Bad JSON becomes a retryable error.
2. **Deterministic validators** (`docupdate/validators.py`). Their error strings are sent back to
   the model verbatim on retry:
   - IDs: one entry per block given, no unknown or duplicate IDs. The plan's IDs must exist.
   - Structure: block kind is preserved (a heading stays a heading at the same level and on one
     line; a table keeps its column count; a list stays a list). No leaked `[B12]` markers, no
     "Here is the revised…" preamble, no code-fence wrapping.
   - **Style-only edits must not change facts:** the multiset of numbers is identical, links are
     preserved, there are no deletions, and the word count stays within 0.5–2×. This catches the
     model "helpfully" editing prices during a tone pass, or merging repeated sentences.
   - Arithmetic: for percentage changes, every new money value must equal an old value × factor,
     to the cent.
3. **LLM verifier** for targeted changes only: one call per job, returning a verdict per change.
   It sees only the before/after of the touched blocks, so its cost is O(edit). It is skipped when
   code has already proved the edit: exact percentage scaling with nothing else touched.
4. **Granular retry and atomicity.** A style-only block is accepted on its own as soon as it
   passes. All blocks of a targeted change are accepted all-or-nothing, *across* chunks. Changes
   that share no blocks are independent units even inside one call, so on a small document where
   all changes share one call, one rejected change does not block the others. If any
   block of "raise prices 10%" fails after retries, every block of that change reverts, so the
   document never contains half a price change. One exception, on the last attempt only: if the
   only blocks still failing are **headings**, they are dropped from the change and kept
   unchanged, and the rest is judged on its own. The planner sometimes points at "## Summary"
   when it means the paragraph under it, and the editor then keeps pushing content into the
   heading. The same applies to pre-swept optional blocks. Dropped blocks are listed in the report
   (`dropped_blocks`).
5. **Graceful degradation.** Nothing raises. Each change is reported as `applied`, `partial` (a
   style pass where some blocks kept their original text, listed in the report), `failed` (with
   the last errors), or `not_found`. The batch moves on to the next document. Transport errors
   (429/5xx/timeouts) are retried by the OpenAI client (`max_retries=3`). An API error that
   outlasts those retries (an outage, or a deprecated model) is treated like a bad output: it goes
   through the same retry and report path and is listed in `usage.api_errors`. It never crashes
   the document. An earlier version did crash when a model returned 404.

## Test cases run (`tests/run_tests.py`, 13/13 pass)

Live, on `samples/nimbus_guide.pdf`, a generated 10.5k-word casual product guide with planted
traps: a pricing table, the Pro price repeated in the FAQ and Summary, one legacy-API paragraph
next to a v2 section that must survive, links, code, and lists:

1. **Spec example** (pricing +10%, remove legacy paragraph, formal tone). Checks: all applied; the
   new prices are in the table, FAQ *and* Summary, and no old price remains; the legacy paragraph
   is gone and the v2 section is kept; contractions are down by more than 90%; headings are
   preserved apart from the emptied "Legacy API" heading; links are preserved.
2. **Targeted only** (pricing + legacy, no tone). The tone is untouched and output is under 2k
   tokens.
3. **Change with no target** ("blockchain integration"). `not_found`, and the document is
   byte-identical.
4. **Rename across the document** ('Pro' → 'Growth'). No "Pro plan" remains; "Pro tip" is
   correctly left alone.

Offline fault injection, using a scripted fake LLM:

5. Broken JSON, then a demoted heading with a leaked ID, then a good answer: applied on attempt 3.
6. An LLM that always returns garbage: the change is `failed`, the document is unchanged, errors
   are reported, and nothing crashes.
7. The planner hallucinates a block ID: the planner is retried and the change applies.
8. The planner selects a heading plus its paragraph, and the editor keeps stuffing content into
   the heading. After retries the heading is dropped and kept unchanged, and the paragraph edit
   applies.
9. Pre-sweep: the planner selects only the pricing table. The FAQ (same `$79`) joins in round 1
   and is updated. An optional block that always comes back broken is kept unchanged without
   rolling back the change.
10. Small document: two independent changes share one edit call. The reviewer rejects one, which
    fails alone while the other applies.
11. A percentage change that code can prove exactly: applied with no reviewer call.
12. OpenAI unreachable (the real wrapper with a client that always raises): the change is
    `failed`, the original is returned, errors are reported, and nothing crashes.
13. **Known false pass** (below).

Also run once on a real 1-page invoice-agreement PDF (`examples/invoice_input.json`) with 10
changes: parties, dates, invoice number, amounts, an installment schedule, payment terms and
governing law. All 10 applied. Two gaps it exposed are not caught by any check: section numbers
are not renumbered after a section is removed (4 → 6), and PDF extraction merges the two
signature lines into one paragraph.

## Known false pass: wrong change that passes every check

`tests/run_tests.py`, test "KNOWN FALSE PASS". Change: *"Make the tone throughout more formal"*.
The editor returns this for the legacy-API paragraph:

> **Before:** The legacy v1 API is still hanging around for older integrations, but honestly you
> shouldn't build anything new on it. It uses XML responses, doesn't support pagination, and it's
> capped at 60 requests per minute. We're planning to switch it off sometime in 2027…
>
> **After:** The legacy v1 API remains fully supported, and new integrations may be built on it
> with confidence. It uses XML responses, does not support pagination, and is limited to 60
> requests per minute. We are planning to retire it in 2027; customers on v1 may migrate to v2 at
> their convenience…

It is formal, valid JSON, still a paragraph, and keeps every number (1, 60, 2027, 2), the link
and roughly the same length. It is applied as `applied`, yet it reverses the guidance from "don't
build on v1" to "build on v1 with confidence".

**Why it isn't caught:** style-only edits are checked only deterministically, for structure,
numbers, links and length. Meaning-preservation is not verified, because an LLM review of a
global change would see the whole document a second time (about 2× the tokens) for what is
usually a harmless rewrite. The numeric checks guard *facts that are numbers*; negations, modal
verbs ("shouldn't" → "may") and qualifiers are not numbers. The fix is known and the cost is
explicit; see below.

A second, structural blind spot: the sweep only finds stale *literal* values and terms. If the
planner misses a block that says "about eighty dollars" or "our mid tier", nothing flags it.

## Alternatives considered (and why not)

- **Whole-document rewrite in one call.** Simplest, but output tokens are O(doc), so latency is
  minutes, and the model drifts in parts it was never asked to touch. There is also nothing to
  validate against except the entire document. Rejected for the token and latency constraints.
- **Unified diff / line-number patches from the LLM.** LLMs are unreliable with line numbers and
  hunk headers, and one bad offset corrupts everything after it. Block IDs give the same
  "point, don't copy" benefit with a trivially checkable address.
- **Search/replace anchors (Aider-style `old_text → new_text`).** A strong alternative with
  output O(edit), no segmentation needed and fine granularity. Not chosen because the anchor
  must be reproduced exactly and appear exactly once. Boilerplate-heavy documents (repeated
  sentences, which our sample has) make anchors ambiguous, and anchor quoting costs about 2× the
  output of the changed text. Block IDs are unambiguous by construction.
- **Embedding retrieval instead of a planner over the whole doc.** It would cut planner input
  well below 1× doc, but "all tiers"/"everywhere" changes need recall over the whole document,
  and top-k retrieval silently misses the FAQ. Input tokens are cheap and fast compared with
  output, so the whole-doc planner is worth it. Retrieval is the next step for 100k+-word documents.

## Approach switches partway through (and what triggered them)

1. **Whole-chunk retries → per-block acceptance.** The first run made jobs all-or-nothing. One
   bad tone edit (it dropped a number) also failed the unrelated "remove legacy paragraph" change
   in the same chunk, and retries re-sent the whole chunk. It now accepts good blocks
   immediately, retries only the failures, and keeps atomicity per targeted change instead of
   per job. Input tokens dropped from 85k to 41k, and latency from 32s to 17–29s.
2. **Chunks of 1,200 → 700 words.** With about 30 blocks per call the model skipped blocks
   ("missing entries").
3. **The LLM does arithmetic → code does arithmetic.** `gpt-4.1-mini` wrote 79 × 1.1 = **$87.90**
   in the Summary. The LLM verifier passed it; the new deterministic percent check caught it. The
   model then repeated the same mistake on all three retries, even with the expected value in the
   feedback, so the system correctly refused to apply the change. The fix was to stop asking for
   arithmetic: code precomputes `$79 -> 86.90` and the model only places the values.
4. **Added the stale-value sweep** after the planner missed the FAQ's `$79` on every run, despite
   prompting for exhaustiveness.
5. **Restricted the sweep to non-deletion changes.** The term sweep took "API" from "remove the
   paragraph about the legacy API" as a stale term and queued 6 unrelated API blocks for
   follow-up. Deletions cannot leave stale values, so they are now skipped. Orphaned headings
   (whose whole section was deleted) are removed deterministically instead.
6. **Added the heading drop.** A final pre-submission run of the spec example came back with
   pricing `failed`: the planner had picked the "## Summary" heading again, and this time the
   editor stuffed pricing text into it on all three attempts. The rollback was safe but the
   change was lost. Headings are now dropped from a targeted change if they are the only blocks
   still failing on the last attempt.
7. **Optimization pass.** See the next section. Two things it surfaced changed the design: the
   pre-sweep had to stay limited to exact values (names doubled the work), and the nano
   experiment exposed that an API error crashed the run.

## Optimizations (after the first working version)

Each one was measured with the full test suite. Opt-in ones stay off by default because they lost
on accuracy or cost overall.

| Size | Optimization | Effect (measured) | Default |
|---|---|---|---|
| small | Per-document concurrency 8 → 32, plus a process-wide cap of 64 | Spec example 22s → 13s (one wave of 17 chunks instead of three) | on |
| small | Code blocks not sent through the style pass | Fewer tokens and fewer chances to fail validation | on |
| small | Reviewer skipped when code proves the change (exact % scaling) | 1 fewer call per proven change | on |
| medium | Per-change units inside a job, plus one review call per job | Lets changes share calls safely | on |
| medium | Small docs (≤ 2,000 words): one edit call for all targeted changes | Invoice: 17 calls / 7.3k in → **3 calls / 3.7k in** | on |
| medium | Pre-sweep exact values into round 1 (optional blocks) | Removes the sequential sweep round for value changes; rename ~30s → ~9s | on |
| big | Cheaper model for style chunks (`DOCUPDATE_STYLE_MODEL`) | `gpt-4.1-nano`: deprecated (404). `gpt-5.4-nano` / `gpt-5-nano`: more validation failures and retries, so 35–40 calls, 49k in, 17–19s and the tone change `partial`. **Worse overall.** | off |
| big | Outline planner (`DOCUPDATE_PLANNER=outline`): headings and tables in full, other blocks as their opening 25 words plus the values and names they mention | Planner input 13.3k → 6.0k (−55%), suite 12/12. A probe of 4 mid-paragraph details found 3/4 (missed "exponential backoff"); with a full-document fallback for `not_found` changes it found 4/4, but cost 19.3k planner tokens on that probe. | off |

Why the outline planner stays off: the spec grades accuracy first and tokens "within an order of
magnitude". A 2× saving on one stage doesn't justify the recall risk. The fallback still can't
catch a *partial* miss (some blocks found, one missed). It is the right default only once a
labelled eval set shows its recall holds.

Not done:
- **Batch API.** The pipeline is multi-round (plan → edit → review → retry), and each Batch round
  can take up to 24h. It fits only as a "plan + first edit pass" batch with synchronous repair of
  failures. That's a different execution mode.
- **Starting the style pass before the planner returns.** It would save about 1–2s, but blocks
  touched by both a style change and a targeted change would then need sequencing.

## What I'd do differently with more time

- **Cheap meaning check for style passes.** This fixes the known false pass: an NLI or embedding
  similarity per block (before vs after), with an LLM review only for low-similarity or
  negation-flipped blocks. That costs O(suspicious blocks), not O(doc).
- **A real eval set.** Many documents and change types with labelled expected output, measuring
  precision and recall of the planner's block selection separately from edit quality. Today's
  checks are hand-written per test.
- **Change ordering semantics.** Changes are applied as one combined pass. Dependent changes
  ("rename X", then "remove the section about the renamed X") would need a sequential plan.
- **Handle sub-block granularity for huge blocks** (long tables): edit rows rather than the whole table.
- **Output fidelity for PDF:** the output PDF is re-rendered from Markdown, so the original layout,
  fonts and images are lost. Use a layout-preserving redaction-and-insert approach, or emit DOCX.
- **Batch mode:** run many documents with a global concurrency and token budget, using OpenAI's
  Batch API (50% cheaper, latency-insensitive). Cache the planner's static prefix.
- **An eval set, then revisit the opt-ins.** The outline planner and the cheaper style models
  were judged on a small suite and one recall probe. A labelled set of documents and changes
  would show whether either can become the default. A *stronger* planner model is worth testing
  too, since planner recall is the accuracy bottleneck.
- **Renumber sections after a deletion,** and keep line breaks inside signature blocks during
  extraction. Both were found on the invoice.

## Layout

```
docupdate/
  blocks.py      segmentation, block ids, splice back
  pdf_io.py      PDF -> Markdown (header/footer strip, page-break stitching), Markdown -> PDF
  llm.py         OpenAI wrapper: strict JSON schema, concurrency cap, token accounting
  validators.py  deterministic checks, percent arithmetic, stale-value diff
  pipeline.py    plan / edit / verify / retry / sweep / apply
  cli.py, api.py
tests/
  make_sample_doc.py   deterministic 10.5k-word sample (md + pdf)
  run_tests.py         live + offline fault-injection tests -> out/tests/results.json
samples/               nimbus_guide.pdf/.md, extracted markdown
```
- `examples/`: real output of the spec example run (updated.md/.pdf, report.json), test results, and a tiny `input.json` in the spec shape.
