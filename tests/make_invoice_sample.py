"""Builds a short fill-in-the-blanks payment agreement (original text, ~500 words) as Markdown + PDF.

  python tests/make_invoice_sample.py   -> samples/payment_agreement.md, samples/payment_agreement.pdf

Exercises what a template needs: [placeholders] repeated across sections, a money table whose totals
depend on each other, percentages, and a signature block that must stay consistent with the parties.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from docupdate.pdf_io import markdown_to_pdf  # noqa: E402

DOC = """# Service Payment Agreement

**Provider:** [Provider name] **Client:** [Client name] **Effective date:** [Date]

## 1. Services and Amount

The Client agrees to pay the Provider for [description of services] as set out in Invoice No. [Invoice number], issued on [Invoice date]. The amounts below are in US dollars and exclude any applicable sales tax.

| Item | Hours | Amount |
|---|---|---|
| Strategy workshop | 8 | $1,200.00 |
| Implementation support | 20 | $3,000.00 |
| Subtotal | | $4,200.00 |
| Total due | | $4,200.00 |

## 2. Payment Schedule

[State whether the total is paid in one sum or in installments, with the amount and due date of each payment.]

## 3. How to Pay

Payments are made by [payment methods]. [Describe any card or transfer fee the Client must cover, or delete this sentence if none applies.]

## 4. Late Payments

If a payment is more than [number] days overdue, the Client owes a late charge of [late charge] on the unpaid balance, to the extent permitted by law. The Provider may pause work on any open request until overdue amounts are settled.

## 5. Questions About an Invoice

The Client must report any disagreement about an invoice in writing within [number] days of receiving it. Undisputed portions of an invoice remain payable on the original schedule.

## 6. Cancellation

Either party may end this agreement with [notice period] written notice. The Client remains responsible for services delivered before the end date.

## 7. Governing Law

This agreement is governed by the laws of [State or country].

## Signatures

Each party confirms that it has read and accepts the terms above.

**[Provider name]** (Provider)

Signature: ______________________  Date: __________

**[Client name]** (Client)

Signature: ______________________  Date: __________
"""

if __name__ == "__main__":
    os.makedirs("samples", exist_ok=True)
    with open("samples/payment_agreement.md", "w") as f:
        f.write(DOC)
    markdown_to_pdf(DOC, "samples/payment_agreement.pdf", footer_pages=False)
    print(f"samples/payment_agreement.md, samples/payment_agreement.pdf ({len(DOC.split())} words)")
