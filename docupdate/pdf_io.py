"""PDF <-> Markdown.

In:  PyMuPDF4LLM turns the PDF into Markdown (headings inferred from font size,
     tables as pipe tables). We then strip running headers/footers/page numbers
     and re-join paragraphs that were split across a page break.
Out: a simple reportlab renderer turns the updated Markdown back into a PDF.
     Layout fidelity with the original PDF is explicitly out of scope.
"""
from __future__ import annotations

import re
from collections import Counter

PAGE_NUM_RE = re.compile(r"^\s*(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*$", re.I)


def _norm(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip().strip("*_ ").lower())


def pdf_to_markdown(path: str) -> str:
    import pymupdf4llm

    pages = [p["text"] for p in pymupdf4llm.to_markdown(path, page_chunks=True)]

    # Lines that sit at the top/bottom edge of many pages are running headers/footers.
    edge_counts: Counter[str] = Counter()
    for t in pages:
        lines = [l for l in t.splitlines() if l.strip()]
        edge_counts.update({_norm(l) for l in lines[:2] + lines[-2:]})
    threshold = max(2, len(pages) // 2)
    boilerplate = {k for k, v in edge_counts.items() if v >= threshold}

    cleaned = []
    for t in pages:
        lines = t.splitlines()
        idx = [i for i, l in enumerate(lines) if l.strip()]
        edges = set(idx[:2] + idx[-2:])
        keep = [
            l for i, l in enumerate(lines)
            if not (i in edges and (_norm(l) in boilerplate or PAGE_NUM_RE.match(l.strip("*_ "))))
        ]
        cleaned.append("\n".join(l.rstrip() for l in keep).strip())

    md = ""
    for page in cleaned:
        if not page:
            continue
        # paragraph continues across the page break: previous page ends mid-sentence,
        # next page starts lower-case
        if md and not re.search(r"[.!?:;)\]|`*]\s*$", md) and re.match(r"^[a-z(]", page):
            md = md.rstrip() + " " + page.lstrip()
        else:
            md = (md + "\n\n" + page) if md else page
    return md.strip() + "\n"


# ---------------------------------------------------------------- Markdown -> PDF

def _inline(text: str) -> str:
    from xml.sax.saxutils import escape

    t = escape(text)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" color="blue">\1</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", t)
    t = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', t)
    return t


def markdown_to_pdf(md: str, out_path: str, header: str = "", footer_pages: bool = True) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle

    from .blocks import LIST_RE, heading_level, split_blocks

    ss = getSampleStyleSheet()
    hstyles = {1: ss["Title"], 2: ss["Heading1"], 3: ss["Heading2"], 4: ss["Heading3"], 5: ss["Heading4"], 6: ss["Heading5"]}
    story = []
    for b in split_blocks(md):
        if b.kind == "heading":
            story.append(Paragraph(_inline(b.text.lstrip("#").strip()), hstyles[heading_level(b.text)]))
        elif b.kind == "list":
            items = [LIST_RE.sub("", l, count=1) for l in b.text.splitlines() if l.strip()]
            numbered = bool(re.match(r"^\s*\d", b.text))
            story.append(ListFlowable([ListItem(Paragraph(_inline(i), ss["BodyText"])) for i in items],
                                      bulletType="1" if numbered else "bullet", start="1" if numbered else None))
        elif b.kind == "table":
            rows = [[Paragraph(_inline(c.strip()), ss["BodyText"]) for c in l.strip().strip("|").split("|")]
                    for l in b.text.splitlines() if l.strip() and not re.match(r"^\s*\|?[\s:|-]+\|?\s*$", l)]
            t = Table(rows, hAlign="LEFT")
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
            story += [t, Spacer(1, 6)]
        elif b.kind == "code":
            story.append(Preformatted("\n".join(b.text.splitlines()[1:-1]), ss["Code"]))
        else:
            story.append(Paragraph(_inline(b.text.replace("\n", " ")), ss["BodyText"]))

    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        if header:
            canvas.drawString(2 * cm, A4[1] - 1.2 * cm, header)
        if footer_pages:
            canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm, f"Page {doc.page}")
        canvas.restoreState()

    SimpleDocTemplate(out_path, pagesize=A4, topMargin=2 * cm, bottomMargin=2 * cm).build(
        story, onFirstPage=decorate, onLaterPages=decorate)
