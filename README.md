# LLM-Driven Document Updater

Applies plain-English edit requests to large PDF or Markdown documents by changing only the affected
sections. Built-in validation and rollback mean a bad edit is reported instead of being silently applied.

Python, FastAPI, OpenAI API (`gpt-4.1-mini` by default).

## 1. Install (Python 3.11+)

```bash
git clone https://github.com/realme72/LLM-Document-Update-.git
cd LLM-Document-Update-
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then set OPENAI_API_KEY=sk-... in .env
```

## 2. Run from the command line

On a PDF, with changes inline (repeat `-c`; use single quotes so `$` isn't expanded):

```bash
.venv/bin/python -m docupdate.cli --pdf samples/nimbus_guide.pdf \
  -c 'Update the pricing section to reflect a 10% increase across all tiers' \
  -c 'Remove the paragraph about the legacy API' \
  -o out/run1
```

Other inputs:

```bash
# changes from a JSON file: ["change one", "change two"]
.venv/bin/python -m docupdate.cli --pdf my.pdf --changes-file changes.json -o out/run2

# Markdown or plain text
.venv/bin/python -m docupdate.cli --md my.md -c 'Make the tone more formal' -o out/run3

# JSON input: {"sourceDocument": "<full text>", "changes": ["..."]}
.venv/bin/python -m docupdate.cli --input examples/input.json -o out/run4

# a one-page payment agreement with 12 changes (placeholders, totals, a deletion, one change with no target)
.venv/bin/python -m docupdate.cli --input examples/agreement_input.json -o out/agreement
```

To make a JSON input from a PDF, run
`.venv/bin/python tests/pdf_to_input.py my.pdf my_input.json` and edit its `"changes"` list.

The output folder contains `updated.md`, `updated.pdf` (for PDF input), `source.md` (the extracted
original) and `report.json` (status of each change, tokens and latency). Each change ends as
`applied`, `partial`, `failed` or `not_found`.

Options: `--model <name>` switches the model; `--no-verify` skips the LLM reviewer.

## 3. Run as an API

```bash
.venv/bin/uvicorn docupdate.api:app --port 8001 --reload
```

Open http://127.0.0.1:8001/docs to try it in the browser.

- `POST /update`: JSON body `{"sourceDocument": "...", "changes": ["..."], "verify": true}`
- `POST /update/pdf`: form upload with `file` (PDF) and `changes` (a JSON list, or one change per line)

```bash
curl -X POST localhost:8001/update/pdf -F file=@samples/payment_agreement.pdf \
  -F 'changes=["The agreement is governed by the laws of the State of New York."]'
```

If you see `address already in use`, choose another port with `--port`.

## 4. Run the tests

```bash
.venv/bin/python tests/run_tests.py --offline   # no API key needed
.venv/bin/python tests/run_tests.py             # also runs live tests (makes OpenAI calls)
```

`tests/make_sample_doc.py` rebuilds the ~10.5k-word guide and `tests/make_invoice_sample.py` the payment
agreement in `samples/`.

## Configuration (optional, in `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DOCUPDATE_MODEL` | `gpt-4.1-mini` | Model used for all calls |
| `DOCUPDATE_CONCURRENCY` | `32` | Max LLM calls in flight per document |
| `DOCUPDATE_GLOBAL_CONCURRENCY` | `64` | Max LLM calls in flight across the process |
| `DOCUPDATE_MAX_PDF_MB` | `20` | API: largest PDF upload accepted |
| `DOCUPDATE_MAX_CHARS` | `500000` | API: longest document text accepted (~80k words) |
| `DOCUPDATE_MAX_CHANGES` | `50` | API: most changes per request |
