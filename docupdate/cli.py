"""CLI.

  python -m docupdate.cli --pdf samples/nimbus_guide.pdf -c "Remove the paragraph about the legacy API" -o out/run1
  python -m docupdate.cli --input input.json -o out/run2       # {"sourceDocument": "...", "changes": [...]}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from dotenv import load_dotenv

from .llm import LLM
from .pdf_io import markdown_to_pdf, pdf_to_markdown
from .pipeline import update_document


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Apply natural-language changes to a document with an LLM.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pdf", help="source PDF")
    src.add_argument("--md", help="source Markdown/text file")
    src.add_argument("--input", help='JSON file {"sourceDocument": str, "changes": [str]}')
    ap.add_argument("-c", "--change", action="append", default=[], help="a change (repeatable)")
    ap.add_argument("--changes-file", help="JSON list of changes")
    ap.add_argument("-o", "--out", default="out/run", help="output directory")
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-verify", action="store_true", help="skip the LLM reviewer pass")
    a = ap.parse_args()

    changes = list(a.change)
    if a.input:
        data = json.load(open(a.input))
        source, changes = data["sourceDocument"], changes + data["changes"]
    elif a.pdf:
        source = pdf_to_markdown(a.pdf)
    else:
        source = open(a.md).read()
    if a.changes_file:
        changes += json.load(open(a.changes_file))
    if not changes:
        ap.error("no changes given")

    os.makedirs(a.out, exist_ok=True)
    open(os.path.join(a.out, "source.md"), "w").write(source)
    res = asyncio.run(update_document(source, changes, LLM(a.model), verify=not a.no_verify))
    open(os.path.join(a.out, "updated.md"), "w").write(res["updatedDocument"])
    json.dump(res["report"], open(os.path.join(a.out, "report.json"), "w"), indent=2)
    if a.pdf:
        markdown_to_pdf(res["updatedDocument"], os.path.join(a.out, "updated.pdf"))

    r, u = res["report"], res["report"]["usage"]
    print(f"{r['words']} words / {r['blocks']} blocks  |  {r['seconds']}s  |  {u['calls']} calls  |  "
          f"in {u['prompt_tokens']} (cached {u['cached_tokens']})  out {u['completion_tokens']} tokens  [{u['model']}]")
    for c in r["changes"]:
        print(f"  [{c['status']:>9}] #{c['index']} {c['change']}  -> {c.get('scope', '')} {c.get('blocks', '')}")
        for e in c.get("errors", []) or c.get("detail", []):
            print(f"              ! {e}")
    print(f"output: {a.out}/")


if __name__ == "__main__":
    main()
