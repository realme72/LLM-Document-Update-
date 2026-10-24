"""Turn a PDF into a spec-shaped input file you can edit by hand.

  python tests/pdf_to_input.py samples/invoice-payment-agreement-template.pdf examples/invoice_input.json
  -> {"sourceDocument": "<extracted markdown>", "changes": ["..."]}   (edit "changes", then run the CLI)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from docupdate.pdf_io import pdf_to_markdown  # noqa: E402

src, dst = sys.argv[1], sys.argv[2]
json.dump({"sourceDocument": pdf_to_markdown(src), "changes": ["<write your change here>"]},
          open(dst, "w"), indent=2, ensure_ascii=False)
print(f"wrote {dst} - edit its \"changes\" list, then:\n  .venv/bin/python -m docupdate.cli --input {dst} -o out/mine")
