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
.venv/bin/python tests/run_tests.py            # 5 offline fault-injection tests + 4 live tests (~1-2 min, OpenAI calls)
.venv/bin/python tests/run_tests.py --offline  # offline only: scripted fake LLM, no API key or cost
```

Each test prints PASS/FAIL with time, number of calls and tokens. Results are written to
`out/tests/results.json`. A saved copy from our run is in `examples/test_results.json`.

## Architecture

```
PDF ──pymupdf4llm──▶ Markdown ──split──▶ blocks [B1..Bn] (id, kind, section path)
                                            │
   1. PLAN    1 call, whole doc in, tiny JSON out: change -> block ids | "global" | "not_found"
   2. EDIT    parallel calls, each sees ONLY its blocks, returns {id, replace|delete|keep, text}
   3. CHECK   deterministic validators per block
   4. VERIFY  small LLM review of before/after for targeted changes (+ deterministic arithmetic check)
   5. RETRY   only the failed blocks, with the validator errors as feedback (max 2 retries)
   6. SWEEP   values/terms a change replaced ("$79", "Pro") still present elsewhere -> follow-up edit
   7. APPLY   code splices accepted edits back in; drops headings whose whole section was deleted
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
- **Stale-value sweep.** After a targeted edit, code diffs the before/after money and percent
  values (`$79` → `$86.90`), plus any terms the change names (`'Pro'`). It then greps the rest of
  the document for the old values. Each hit gets a small follow-up edit, which may answer `keep`
  if the match is unrelated (e.g. "Pro tip"). In testing this caught the FAQ's `$79` and the
  `Pro` in "everything in Pro", both of which the planner missed.

**Why this shape — token and latency scaling.** Output tokens are the expensive, slow part
(roughly 50–100 tok/s). A naive full rewrite has output O(doc): about 14k tokens and minutes per
10k-word document, every time, even to change one sentence. Here:

| | input tokens | output tokens | wall time |
|---|---|---|---|
| Planner (always) | ≈ 1× doc (~13k) | O(#changes), ~30–300 | 1–3 s |
| Targeted edits | O(touched blocks) | O(edit size) | parallel, ~2–5 s |
| Global style change | ≈ 1× doc, chunked | ≈ 1× doc (inherent: every sentence changes) | **one chunk's** latency: chunks of ~700 words run in parallel |

Measured on the 10.5k-word sample (`gpt-4.1-mini`):

| run | calls | in | out | time |
|---|---|---|---|---|
| pricing +10% + remove legacy paragraph | 5 | 15.9k | **0.57k** | 5.8 s |
| rename "Pro" → "Growth" everywhere | 5 | 22.5k | 3.1k | ~33 s* |
| spec example (pricing + legacy + formal tone) | 32 | 41.5k | 14.2k | 29.5 s |
| change that matches nothing | 1 | 13.3k | 29 | 1.2 s |

\*The sweep follow-up runs after the main edits, so it adds a sequential round.

Targeted changes cost about 1× the document as input and almost nothing as output. Only a
whole-document style change pays O(doc) output. That cost is unavoidable, but it is parallelized,
so latency stays flat as the document grows. Unchanged blocks in a style pass can answer `keep`
instead of repeating their text. Retries re-send only the failing blocks, not the chunk or the
document.

**Model choice.** `gpt-4.1-mini` is a non-reasoning model, so there are no hidden reasoning tokens
and latency is predictable. It supports strict JSON-schema outputs and long context, and it is
cheap. Override with `DOCUPDATE_MODEL` or `--model`. Arithmetic is not left to the model at all:
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
3. **LLM verifier** for targeted changes only. It sees only the before/after of the touched
   blocks, so its cost is O(edit).
4. **Granular retry and atomicity.** A style-only block is accepted on its own as soon as it
   passes. All blocks of a targeted change are accepted all-or-nothing, *across* chunks. If any
   block of "raise prices 10%" fails after retries, every block of that change reverts, so the
   document never contains half a price change. One exception, on the last attempt only: if the
   only blocks still failing are **headings**, they are dropped from the change and kept
   unchanged, and the rest is judged on its own. The planner sometimes points at "## Summary"
   when it means the paragraph under it, and the editor then keeps pushing content into the
   heading. Dropped headings are listed in the report (`dropped_headings`).
5. **Graceful degradation.** Nothing raises. Each change is reported as `applied`, `partial` (a
   style pass where some blocks kept their original text, listed in the report), `failed` (with
   the last errors), or `not_found`. The batch moves on to the next document. Transport errors
   (429/5xx/timeouts) are retried by the OpenAI client (`max_retries=3`).

## Test cases run (`tests/run_tests.py`, 9/9 pass)

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
9. **Known false pass** (below).

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
- **Fewer calls for small documents.** Every edit and review call carries a fixed system prompt
  (about 300 and 150 tokens respectively), so on short documents that overhead dominates. In a
  run on a 1-page invoice (about 450 tokens) with 10 changes, the changes formed 8 independent
  groups, giving 17 calls and 7.3k input tokens: about 16× the document, nearly all of it prompt
  overhead. Overhead grows with the number of changes, not with document size. On the
  10.5k-word sample, input was about 1.2× the document. Two fixes:
  - Below a size threshold, merge all targeted groups into **one edit call**. Per-change
    validation and all-or-nothing acceptance are kept by validating each group's blocks
    separately inside the combined response. Saves about 2.5k tokens on that run.
  - Review all groups in **one reviewer call** that returns a verdict per change. Saves about 2k.

  Together these bring that run to about 2.5–3k input tokens. The cost is giving up some
  parallelism, which is negligible for small documents, plus a larger blast radius when a
  combined response is malformed: a bad response would need a retry of more blocks.
- **Model per stage:** benchmark `gpt-4.1-nano` for style chunks and a stronger model only for
  the planner, since planner recall is the accuracy bottleneck.

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
