"""Deterministic checks on LLM output. Each returns a list of human-readable
error strings; those strings are fed back to the model verbatim on retry."""
from __future__ import annotations

import json
import re
from collections import Counter

from .blocks import ID_LEAK_RE, Block, classify, heading_level, table_columns

URL_RE = re.compile(r"https?://[^\s)\]]+")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
# "Here is the revised paragraph:" style preambles (but not legitimate prose like "Here is an overview of...")
CHATTER_RE = re.compile(r"^\s*(sure|certainly|of course|here is|here's|below is)\b[^\n]{0,60}\b(revised|updated|rewritten|edited|new version|formal version)\b", re.I)
VALUE_RE = re.compile(r"[$€£₹]\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?\s?%")


def parse_json(raw: str) -> tuple[dict | None, list[str]]:
    try:
        return json.loads(raw), []
    except (json.JSONDecodeError, TypeError) as e:
        return None, [f"Output was not valid JSON ({e}). Return only the JSON object."]


def validate_plan(plan: dict, n_changes: int, valid_ids: set[str]) -> list[str]:
    errs = []
    seen = Counter(p["change_index"] for p in plan.get("plan", []))
    for i in range(n_changes):
        if seen[i] != 1:
            errs.append(f"change_index {i} must appear exactly once (appeared {seen[i]} times).")
    for p in plan.get("plan", []):
        if not 0 <= p["change_index"] < n_changes:
            errs.append(f"change_index {p['change_index']} does not exist.")
        bad = [b for b in p["block_ids"] if b not in valid_ids]
        if bad:
            errs.append(f"change {p['change_index']}: unknown block ids {bad}.")
        if p["scope"] == "local" and not p["block_ids"]:
            errs.append(f"change {p['change_index']}: scope 'local' requires at least one block id.")
    return errs


def _nums(text: str) -> Counter:
    return Counter(n.replace(",", "") for n in NUM_RE.findall(text))


def validate_block_edit(orig: Block, action: str, text: str, *, local: bool) -> list[str]:
    """local=True if a targeted (content) change applies to this block; False if only
    a global style change does, in which case facts must be preserved exactly."""
    p = f"{orig.id}:"
    if action == "keep":
        return []
    if action == "delete":
        return [] if local else [f"{p} style-only changes must not delete content."]
    t = text.strip()
    if not t:
        return [f"{p} action 'replace' with empty text; use 'delete' or 'keep'."]
    errs = []
    if ID_LEAK_RE.search(t):
        errs.append(f"{p} text contains a block id marker like [B12]; remove it.")
    if CHATTER_RE.match(t):
        errs.append(f"{p} text starts with commentary; return only the document text.")
    if t.startswith("```") and orig.kind != "code":
        errs.append(f"{p} text is wrapped in a code fence; return raw Markdown.")

    new_kind = classify(t.split("\n\n")[0])
    if new_kind != orig.kind:
        errs.append(f"{p} block was a {orig.kind} but output is a {new_kind}; preserve the Markdown structure.")
    elif orig.kind == "heading":
        if heading_level(t) != heading_level(orig.text):
            errs.append(f"{p} heading level changed from {heading_level(orig.text)} to {heading_level(t)}.")
        if "\n" in t:
            errs.append(f"{p} heading must stay a single line.")
    elif orig.kind == "table" and table_columns(t) != table_columns(orig.text):
        errs.append(f"{p} table column count changed {sorted(table_columns(orig.text))} -> {sorted(table_columns(t))}.")
    elif orig.kind == "code" and not local and t != orig.text.strip():
        errs.append(f"{p} code blocks must not be modified by style changes.")

    if not local:
        missing_urls = set(URL_RE.findall(orig.text)) - set(URL_RE.findall(t))
        if missing_urls:
            errs.append(f"{p} links removed: {sorted(missing_urls)}.")
        if _nums(orig.text) != _nums(t):
            lost = sorted((_nums(orig.text) - _nums(t)).elements())
            added = sorted((_nums(t) - _nums(orig.text)).elements())
            errs.append(f"{p} style-only change altered numbers (removed {lost}, added {added}); keep every number exactly.")
        if orig.words >= 8 and not 0.5 <= len(t.split()) / orig.words <= 2.0:
            errs.append(f"{p} length changed from {orig.words} to {len(t.split())} words; a style change should not add or drop content.")
    return errs


def changed_values(old: str, new: str) -> set[str]:
    """Money/percent values present before an edit and gone after it (e.g. '$79' after a price change)."""
    return set(VALUE_RE.findall(old)) - set(VALUE_RE.findall(new))


PCT_CHANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s?%\s*(increase|decrease|higher|lower|raise|rise|cut|reduction|discount|more|less)", re.I)
MONEY_RE = re.compile(r"[$€£₹]\s?(\d[\d,]*(?:\.\d+)?)")


def _pct_factor(change: str) -> tuple[float, str] | None:
    m = PCT_CHANGE_RE.search(change)
    if not m:
        return None
    pct = float(m.group(1)) / 100
    down = m.group(2).lower() in ("decrease", "lower", "cut", "reduction", "discount", "less")
    return (1 - pct if down else 1 + pct), m.group(0)


def percent_hint(change: str, texts: list[str]) -> str:
    """Do the arithmetic in code, not in the LLM: a lookup table of old -> new amounts."""
    f = _pct_factor(change)
    if not f:
        return ""
    vals = sorted({v for t in texts for v in MONEY_RE.findall(t)}, key=lambda v: float(v.replace(",", "")))
    if not vals:
        return ""
    pairs = [f"{v} -> {round(float(v.replace(',', '')) * f[0], 2):,.2f}" for v in vals]
    return " (pre-computed amounts, use exactly: " + "; ".join(pairs) + ")"


def check_percent_change(change: str, orig: Block, new: str | None) -> list[str]:
    """Change-specific arithmetic check: for 'N% increase/decrease' every *new* money value in
    the block must be an original money value scaled by that factor (to the cent)."""
    f = _pct_factor(change)
    if not f or new is None:
        return []
    factor, phrase = f
    old_vals = {float(v.replace(",", "")) for v in MONEY_RE.findall(orig.text)}
    added = {float(v.replace(",", "")) for v in MONEY_RE.findall(new)} - old_vals
    expected = {round(v * factor, 2) for v in old_vals}
    bad = sorted(v for v in added if not any(abs(v - e) <= 0.011 for e in expected))
    if bad:
        return [f"{orig.id}: new amounts {bad} are not a {phrase} of any original amount "
                f"(expected values from {sorted(expected)})."]
    return []


def percent_proven(change: str, orig: Block, new: str | None) -> bool:
    """True if the edit is exactly 'every amount scaled by the change's factor, nothing else touched'.
    Code can prove that on its own, so the LLM reviewer is not needed."""
    f = _pct_factor(change)
    if not f or new is None:
        return False

    def scaled(m):
        v = float(m.group(1).replace(",", ""))
        return m.group(0)[0] + f"{round(v * f[0], 2):,.2f}"

    return MONEY_RE.sub(scaled, orig.text).strip() == new.strip()
