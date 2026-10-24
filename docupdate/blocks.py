"""Deterministic document segmentation: Markdown -> addressable blocks.

Every block gets a stable ID (B1, B2, ...) so the LLM can *point at* text
instead of reproducing it. That is what keeps output tokens proportional to
the size of the change rather than the size of the document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

HEADING_RE = re.compile(r"^(#{1,6})\s+\S")
LIST_RE = re.compile(r"^\s*([-*+•]|\d+[.)])\s+")
ID_LEAK_RE = re.compile(r"\[B\d+\]")


@dataclass
class Block:
    id: str
    kind: str  # heading | paragraph | list | table | code
    text: str
    path: str = ""  # section breadcrumb, e.g. "Pricing > Enterprise"

    @property
    def words(self) -> int:
        return len(self.text.split())


def classify(text: str) -> str:
    t = text.strip()
    if not t:
        return "empty"
    if t.startswith("```"):
        return "code"
    first = t.splitlines()[0]
    if HEADING_RE.match(first):
        return "heading"
    lines = [l for l in t.splitlines() if l.strip()]
    if all(l.lstrip().startswith("|") for l in lines):
        return "table"
    if LIST_RE.match(first):
        return "list"
    return "paragraph"


def heading_level(text: str) -> int:
    m = HEADING_RE.match(text.strip())
    return len(m.group(1)) if m else 0


def table_columns(text: str) -> set[int]:
    return {len(l.strip().strip("|").split("|")) for l in text.splitlines() if l.strip()}


def split_blocks(md: str) -> list[Block]:
    raw: list[str] = []
    buf: list[str] = []
    in_code = False

    def flush():
        if buf:
            raw.append("\n".join(buf).strip("\n"))
            buf.clear()

    for line in md.splitlines():
        if line.strip().startswith("```"):
            if in_code:
                buf.append(line)
                flush()
                in_code = False
            else:
                flush()
                in_code = True
                buf.append(line)
            continue
        if in_code:
            buf.append(line)
            continue
        if not line.strip():
            flush()
            continue
        if HEADING_RE.match(line):
            flush()
            raw.append(line.strip())
            continue
        # a table glued to a paragraph (no blank line between) is still its own block
        if buf and line.lstrip().startswith("|") != buf[-1].lstrip().startswith("|"):
            flush()
        buf.append(line)
    flush()

    # PDF extraction often puts a blank line between list items; re-group them into one list block
    merged: list[str] = []
    for t in (t for t in raw if t.strip()):
        if merged and classify(t) == "list" and classify(merged[-1]) == "list":
            merged[-1] += "\n" + t
        else:
            merged.append(t)

    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []
    for i, text in enumerate(merged):
        kind = classify(text)
        if kind == "heading":
            lvl = heading_level(text)
            stack = [s for s in stack if s[0] < lvl] + [(lvl, text.lstrip("#").strip().strip("*"))]
        blocks.append(Block(id=f"B{i + 1}", kind=kind, text=text, path=" > ".join(s[1] for s in stack)))
    return blocks


def render_for_llm(blocks: list[Block]) -> str:
    return "\n\n".join(f"[{b.id}] {b.text}" for b in blocks)


def join_blocks(blocks: list[Block], replacements: dict[str, str | None]) -> str:
    """replacements: id -> new text, or None to delete. Missing ids are kept."""
    out = []
    for b in blocks:
        if b.id in replacements:
            new = replacements[b.id]
            if new is None:
                continue
            out.append(new.strip())
        else:
            out.append(b.text)
    return "\n\n".join(out) + "\n"
