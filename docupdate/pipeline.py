"""Plan -> pre-sweep -> edit -> validate -> verify -> retry -> sweep -> apply.

1. PLAN      (1 call, sees whole doc): map each change to block IDs, or 'global', or 'not_found'.
2. PRE-SWEEP (code): other blocks stating the same prices/percentages/[placeholders] join the
             change as optional blocks, so planner misses are fixed in the first round.
3. EDIT      (parallel calls): each job sees only its blocks and returns per-block replace/delete/keep.
             Output ~ size of the edit. Small documents use one call for all targeted changes.
4. CHECK     (code): schema, ids, Markdown structure, numbers/links preserved under style-only
             edits, length sanity, exact arithmetic for percentage changes.
5. VERIFY    (1 call per job): LLM review of before/after of the touched blocks; skipped when code
             already proved the change.
6. RETRY     only the blocks that failed, with the errors as feedback (max MAX_RETRIES). Style-only
             blocks are accepted one by one; each targeted change (with any changes sharing its
             blocks) is all-or-nothing, across jobs. Anything still failing keeps its original
             text and is reported. One bad output never sinks the document.
7. SWEEP     safety net: values/terms a change replaced that still appear elsewhere get one
             follow-up edit call.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field

from .blocks import Block, heading_level, join_blocks, render_for_llm, render_outline, split_blocks
from .llm import LLM
from .validators import VALUE_RE, changed_values, check_percent_change, percent_hint, percent_proven, parse_json, validate_block_edit, validate_plan

MAX_RETRIES = 2
GLOBAL_CHUNK_WORDS = 700
SMALL_DOC_WORDS = 2000  # below this, all targeted changes share one edit call (fixed prompt overhead dominates)
REMOVAL_RE = re.compile(r"\b(remove|delete|drop|strip|cut)\b", re.I)

# ------------------------------------------------------------------ prompts / schemas

PLANNER_SYSTEM = """You map requested changes onto a document. The document is split into blocks; each block starts with an ID like [B12].
For every change return exactly one entry:
- scope "local": list EVERY block that must be modified to fully apply the change. Be exhaustive: if the change concerns facts (prices, names, dates, product names, limits), include every block anywhere in the document that states or depends on those facts (tables, FAQs, summaries, examples) - even when the change names one specific section, the document must stay consistent. For removals, list the blocks to remove (include the heading if a whole section goes).
- scope "global": the change applies to the style/tone/language of the whole document; block_ids = [].
- scope "not_found": nothing in the document matches the change; block_ids = [].
Do not edit anything. reason: at most 12 words."""

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["plan"],
    "properties": {"plan": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["change_index", "scope", "block_ids", "reason"],
        "properties": {
            "change_index": {"type": "integer"},
            "scope": {"type": "string", "enum": ["local", "global", "not_found"]},
            "block_ids": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        }}}},
}

EDITOR_SYSTEM = """You edit selected blocks of a larger Markdown document. You get instructions and a set of blocks, each labelled with its ID and section.
Return exactly one entry per block ID you were given:
- action "replace": text = the full new Markdown for that block.
- action "delete": remove the block entirely; text = "".
- action "keep": block needs no change; text = "".
Rules:
- Apply each instruction only to the blocks it names. Change only what the instructions require; keep every other fact, number, name, link and phrase.
- Blocks were pre-selected automatically and may include a block the instruction does not really apply to (e.g. a heading next to the relevant paragraph). Then leave that block as it is for that instruction - never move content into it from elsewhere.
- Preserve Markdown structure exactly: heading level (#), list markers, table rows/columns and pipes, links, bold/italics, code blocks.
- Never include block IDs like [B12] in the text. No commentary.
- Show computed values (e.g. new prices) with the same formatting conventions the document already uses."""

EDIT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["edits"],
    "properties": {"edits": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "action", "text"],
        "properties": {
            "id": {"type": "string"},
            "action": {"type": "string", "enum": ["replace", "delete", "keep"]},
            "text": {"type": "string"},
        }}}},
}

VERIFIER_SYSTEM = """You audit edits made to part of a document. For each instruction, check the BEFORE/AFTER of the blocks it targeted.
ok = true only if the AFTER text correctly and completely implements the instruction for those blocks (check arithmetic exactly) and makes no unrequested content changes. Ignore changes in tone/wording if a style instruction is also listed.
These blocks are only part of the document and were pre-selected automatically: a block that contains nothing the instruction applies to is correctly left unchanged, and an instruction about "all X" is satisfied if every X that appears in the shown blocks is handled. Judge only what is shown. problem: empty if ok, else one short sentence."""

VERIFY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdicts"],
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["change_index", "ok", "problem"],
        "properties": {"change_index": {"type": "integer"}, "ok": {"type": "boolean"}, "problem": {"type": "string"}},
    }}},
}

# ------------------------------------------------------------------ data


@dataclass
class Job:
    name: str
    blocks: list[Block]
    global_changes: list[int] = field(default_factory=list)          # apply to every block in the job
    local_changes: dict[int, list[str]] = field(default_factory=dict)  # change -> block ids in this job
    optional: set[str] = field(default_factory=set)  # pre-swept blocks: kept unchanged if they fail, never sink the change
    model: str | None = None                          # per-job model override (style-only chunks)

    def is_local(self, block_id: str) -> bool:
        return any(block_id in ids for ids in self.local_changes.values())


@dataclass
class JobResult:
    job: Job
    attempts: int
    replacements: dict[str, str | None]  # accepted edits only (id -> text, None = delete)
    failed: list[str]                    # block ids that never produced an acceptable edit
    errors: list[str]
    dropped: list[str] = field(default_factory=list)  # headings/optional blocks dropped from a change, kept unchanged


# ------------------------------------------------------------------ helpers


def _instructions(job: Job, changes: list[str], ids: set[str], only: set[int] | None = None) -> str:
    by_id = {b.id: b for b in job.blocks}
    lines = [f"- (#{i}) [all blocks] {changes[i]}" for i in job.global_changes]
    for i, bids in job.local_changes.items():
        if only is not None and i not in only:
            continue
        if mine := [b for b in bids if b in ids]:
            hint = percent_hint(changes[i], [by_id[b].text for b in mine])
            req = [b for b in mine if b not in job.optional]
            opt = [b for b in mine if b in job.optional]
            if req:
                lines.append(f"- (#{i}) [only {', '.join(req)}] {changes[i]}{hint}")
            if opt:
                lines.append(f"  (#{i}) also check [{', '.join(opt)}]: they mention the same values or names. Apply "
                             f"change #{i} to each one that refers to the same thing; keep the ones that do not."
                             + ("" if req else f" Change: {changes[i]}{hint}"))
    return "\n".join(lines)


def _chunk(blocks: list[Block], max_words: int) -> list[list[Block]]:
    chunks, cur, n = [], [], 0
    for b in blocks:
        if cur and n + b.words > max_words:
            chunks.append(cur)
            cur, n = [], 0
        cur.append(b)
        n += b.words
    return chunks + ([cur] if cur else [])


def _union(sets: dict[int, set[str]]) -> list[tuple[set[int], set[str]]]:
    """Group changes that share blocks, so no block is edited by two independent units."""
    groups: list[tuple[set[int], set[str]]] = []
    for ci, bids in sets.items():
        cs, bs = {ci}, set(bids)
        for g in [g for g in groups if g[1] & bs]:
            groups.remove(g)
            cs |= g[0]
            bs |= g[1]
        groups.append((cs, bs))
    return groups


def _mentions(text: str, value: str) -> bool:
    return re.search(rf"(?<![\w$]){re.escape(value)}(?![\w.]\d|\w)", text) is not None


def _placeholders(change: str) -> list[str]:
    """Template placeholders a change names, e.g. [Payee name]. Exact like prices, so safe to pre-sweep.
    Plain names ('Pro') are left to the post-edit sweep: they are ambiguous ("Pro tip") and would pull
    every look-alike block into the first round."""
    return re.findall(r"\[[^\]]{2,40}\]", change)


def build_jobs(blocks: list[Block], plan: list[dict]) -> list[Job]:
    global_idx = [p["change_index"] for p in plan if p["scope"] == "global"]
    local = {p["change_index"]: list(dict.fromkeys(p["block_ids"] + p.get("optional_ids", [])))
             for p in plan if p["scope"] == "local"}
    required = {i for p in plan if p["scope"] == "local" for i in p["block_ids"]}
    optional = {i for p in plan for i in p.get("optional_ids", [])} - required
    style_model = os.getenv("DOCUPDATE_STYLE_MODEL") or None

    if global_idx:
        # Style change touches everything: fan out over fixed-size chunks in parallel.
        # Targeted changes ride along with whichever chunk holds their blocks.
        # Code blocks are never restyled, so they are not sent unless a targeted change names them.
        targeted = {i for v in local.values() for i in v}
        style = [b for b in blocks if b.kind != "code" or b.id in targeted]
        jobs = []
        for k, chunk in enumerate(_chunk(style, GLOBAL_CHUNK_WORDS)):
            ids = {b.id for b in chunk}
            lc = {ci: [b for b in bids if b in ids] for ci, bids in local.items()}
            lc = {ci: v for ci, v in lc.items() if v}
            jobs.append(Job(f"chunk{k}", chunk, global_idx, lc, optional & ids, None if lc else style_model))
        return jobs

    groups = _union({ci: set(v) for ci, v in local.items()})
    if groups and sum(b.words for b in blocks) <= SMALL_DOC_WORDS:
        # small document: one edit call for everything; units inside the job keep changes independent
        groups = [(set().union(*(g[0] for g in groups)), set().union(*(g[1] for g in groups)))]
    by_id = {b.id: b for b in blocks}
    order = {b.id: i for i, b in enumerate(blocks)}
    return [Job(f"group{k}", [by_id[b] for b in sorted(bs, key=order.get)], [], {ci: local[ci] for ci in sorted(cs)}, optional & bs)
            for k, (cs, bs) in enumerate(groups)]


def _presweep(blocks: list[Block], plan: list[dict], changes: list[str]) -> None:
    """Before editing: blocks elsewhere that state the same money/percent values or named terms as a
    targeted change's blocks ('$79' in the FAQ) join that change as optional blocks. This catches
    planner misses in the first round instead of a second, sequential sweep round."""
    for p in plan:
        ci = p["change_index"]
        if p["scope"] != "local" or REMOVAL_RE.search(changes[ci]):
            continue  # removals cannot leave stale values behind
        sel = set(p["block_ids"])
        text = " ".join(b.text for b in blocks if b.id in sel)
        keys = set(VALUE_RE.findall(text)) | {t for t in _placeholders(changes[ci]) if t in text}
        hits = [b.id for b in blocks if b.id not in sel and any(_mentions(b.text, k) for k in keys)]
        if hits:
            p["optional_ids"] = hits


# ------------------------------------------------------------------ stages


async def plan_changes(llm: LLM, blocks: list[Block], changes: list[str]) -> tuple[list[dict] | None, int, list[str]]:
    if os.getenv("DOCUPDATE_PLANNER") != "outline":
        return await _plan(llm, blocks, changes, render_for_llm(blocks))
    # Outline planner (~45% of the tokens). An outline can hide a detail mid-paragraph, so any change it
    # reports as not_found is planned again against the full document.
    plan, attempts, errs = await _plan(llm, blocks, changes, render_outline(blocks))
    missing = [p["change_index"] for p in plan or [] if p["scope"] == "not_found"]
    if missing:
        sub, more, _ = await _plan(llm, blocks, [changes[i] for i in missing], render_for_llm(blocks), tag="plan-full")
        if sub:
            redo = {missing[p["change_index"]]: {**p, "change_index": missing[p["change_index"]]} for p in sub}
            plan = [redo.get(p["change_index"], p) for p in plan]
            attempts += more
    return plan, attempts, errs


async def _plan(llm: LLM, blocks: list[Block], changes: list[str], doc: str, tag: str = "plan") -> tuple[list[dict] | None, int, list[str]]:
    user = "DOCUMENT:\n" + doc + "\n\nCHANGES:\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(changes))
    ids = {b.id for b in blocks}
    messages = [{"role": "system", "content": PLANNER_SYSTEM}, {"role": "user", "content": user}]
    errs: list[str] = []
    for attempt in range(1, MAX_RETRIES + 2):
        raw = await llm.chat_json(f"{tag}#{attempt}", messages, PLAN_SCHEMA)
        parsed, errs = parse_json(raw)
        if parsed is not None:
            errs = validate_plan(parsed, len(changes), ids)
        if not errs:
            return parsed["plan"], attempt, []
        # document prefix is unchanged, so the retry hits OpenAI's prompt cache
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": "Your output had these problems:\n- " + "\n- ".join(errs) + "\nReturn the complete corrected JSON."}]
    return None, MAX_RETRIES + 1, errs


def _percent_errors(job: Job, changes: list[str], cand: dict[str, str | None], by_id: dict[str, Block]) -> list[str]:
    return [e for ci, bids in job.local_changes.items() for i in bids if i in cand
            for e in check_percent_change(changes[ci], by_id[i], cand[i])]


def _proven(job: Job, changes: list[str], cs: set[int], cand: dict[str, str | None], by_id: dict[str, Block]) -> bool:
    """Code already proved the unit correct (exact percentage scaling, nothing else touched): skip the reviewer."""
    if job.global_changes:
        return False
    return all(percent_proven(changes[ci], by_id[i], cand[i]) for ci in cs for i in job.local_changes[ci] if i in cand)


async def run_job(llm: LLM, job: Job, changes: list[str], verify: bool) -> JobResult:
    by_id = {b.id: b for b in job.blocks}
    accepted: dict[str, str | None] = {}
    pending = list(by_id)
    feedback: list[str] = []
    dropped: list[str] = []

    def droppable(i: str) -> bool:
        # blocks a targeted change can do without: a heading the planner pointed at by mistake, or a pre-swept block
        return by_id[i].kind == "heading" or i in job.optional

    def drop(ids):
        for i in ids:
            accepted[i] = by_id[i].text
            dropped.append(i)

    attempt = 0
    while pending and attempt <= MAX_RETRIES:
        attempt += 1
        last = attempt > MAX_RETRIES
        user = "INSTRUCTIONS:\n" + _instructions(job, changes, set(pending)) + "\n\nBLOCKS:\n" + "\n\n".join(
            f"[{i}] (section: {by_id[i].path or '-'})\n{by_id[i].text}" for i in pending)
        if feedback:
            user += "\n\nA previous attempt at these blocks was rejected. Fix these problems:\n- " + "\n- ".join(feedback)
        raw = await llm.chat_json(f"{job.name}#{attempt}", [{"role": "system", "content": EDITOR_SYSTEM},
                                                            {"role": "user", "content": user}], EDIT_SCHEMA, model=job.model)
        parsed, feedback = parse_json(raw)
        if parsed is None:
            continue

        edits: dict[str, dict] = {}
        for e in parsed["edits"]:
            if e["id"] not in pending:
                feedback.append(f"{e['id']}: not one of the blocks given; only return the blocks given.")
            elif e["id"] in edits:
                feedback.append(f"{e['id']}: duplicate entry.")
            else:
                edits[e["id"]] = e
        block_errs = {i: ([f"{i}: missing entry; return one entry per block."] if i not in edits else
                          validate_block_edit(by_id[i], edits[i]["action"], edits[i]["text"], local=job.is_local(i)))
                      for i in pending}

        def value(i):
            e = edits[i]
            if e["action"] == "delete":
                return None
            return by_id[i].text if e["action"] == "keep" else e["text"].strip()

        # style-only blocks: accept each good one individually
        for i in pending:
            if not job.is_local(i) and not block_errs[i]:
                accepted[i] = value(i)

        # targeted changes: each unit (changes sharing blocks) is all-or-nothing
        ready: list[tuple[set[int], dict[str, str | None]]] = []
        for cs, bs in _union({ci: {b for b in bids if b in pending} for ci, bids in job.local_changes.items()}):
            ids = [i for i in pending if i in bs]
            if last:
                # last attempt: if only droppable blocks still fail, keep them unchanged and judge the rest
                stuck = [i for i in ids if block_errs[i]]
                if stuck and all(droppable(i) for i in stuck):
                    drop(stuck)
                    for i in stuck:
                        block_errs[i] = []
                    ids = [i for i in ids if i not in stuck]
            if ids and not any(block_errs[i] for i in ids):
                cand = {i: value(i) for i in ids}
                if errs := _percent_errors(job, changes, cand, by_id):
                    feedback += errs
                else:
                    ready.append((cs, cand))

        to_review = [u for u in ready if not _proven(job, changes, u[0], u[1], by_id)] if verify else []
        rejected = await _verify(llm, job, changes, to_review, by_id)
        for cs, cand in ready:
            if not any(ci in rejected for ci in cs):
                accepted.update(cand)
                continue
            keep = {i: v for i, v in cand.items() if not droppable(i)}
            if last and keep and len(keep) < len(cand):
                # last attempt: maybe a droppable block is what the reviewer objected to
                if not any(ci in await _verify(llm, job, changes, [(cs, keep)], by_id) for ci in cs):
                    drop([i for i in cand if i not in keep])
                    accepted.update(keep)
                    continue
            feedback += [f"reviewer rejected change #{ci}: {rejected[ci]}" for ci in cs if ci in rejected]
        feedback += [e for i in pending for e in block_errs[i]]
        pending = [i for i in by_id if i not in accepted]

    changed = {i: v for i, v in accepted.items() if v is None or v.strip() != by_id[i].text.strip()}
    return JobResult(job, attempt, changed, pending, feedback if pending else [], dropped)


async def _verify(llm: LLM, job: Job, changes: list[str], units: list[tuple[set[int], dict[str, str | None]]],
                  by_id: dict[str, Block]) -> dict[int, str]:
    """One review call for all units of a job. Returns {change_index: problem} for rejected changes."""
    if not units:
        return {}
    cand = {i: v for _, c in units for i, v in c.items()}
    cs = set().union(*(u[0] for u in units))
    pairs = [f"[{i}] (section: {by_id[i].path or '-'})\nBEFORE:\n{by_id[i].text}\nAFTER:\n{'<<DELETED>>' if v is None else v}"
             for i, v in cand.items()]
    user = "INSTRUCTIONS:\n" + _instructions(job, changes, set(cand), cs) + "\n\nEDITED BLOCKS:\n" + "\n\n".join(pairs)
    raw = await llm.chat_json(f"{job.name}:verify", [{"role": "system", "content": VERIFIER_SYSTEM},
                                                     {"role": "user", "content": user}], VERIFY_SCHEMA)
    parsed, _ = parse_json(raw)
    if parsed is None:
        return {}  # unreadable review is not evidence of a bad edit; deterministic checks already passed
    return {v["change_index"]: v["problem"] for v in parsed["verdicts"] if not v["ok"] and v["change_index"] in cs}


async def _sweep_stale_values(llm, blocks, plan, replacements, changes, verify) -> list[JobResult]:
    """Safety net after editing: a targeted change replaced e.g. '$79' -> '$86.90'. If '$79' still appears
    in a block the change did not cover, give that block a follow-up edit (it may answer 'keep').
    With the pre-sweep this usually finds nothing, so no extra round runs."""
    by_id = {b.id: b for b in blocks}
    current = {b.id: replacements.get(b.id, b.text) for b in blocks}
    hits_by_change: dict[int, list[str]] = {}
    for p in plan:
        if p["scope"] != "local":
            continue
        edited = [i for i in p["block_ids"] if current[i] is not None and current[i] != by_id[i].text]
        if not edited:
            continue  # pure deletions/no-ops cannot leave stale values behind
        stale = set().union(*[changed_values(by_id[i].text, current[i]) for i in edited] or [set()])
        # terms the change names (quoted or Capitalized) that the edit removed from every block it touched
        before = " ".join(by_id[i].text for i in p["block_ids"])
        after = " ".join(current[i] for i in edited)
        for term in re.findall(r"['\"]([^'\"]{2,40})['\"]|\b([A-Z][A-Za-z0-9-]+)\b", changes[p["change_index"]]):
            term = term[0] or term[1]
            if re.search(rf"\b{re.escape(term)}\b", before) and not re.search(rf"\b{re.escape(term)}\b", after):
                stale.add(term)
        if not stale:
            continue
        # pre-swept blocks the editor changed are done; ones it kept get a focused second look
        covered = set(p["block_ids"]) | {i for i in p.get("optional_ids", []) if current[i] != by_id[i].text}
        hits = [i for i, t in current.items() if t and i not in covered and any(_mentions(t, v) for v in stale)]
        if hits:
            hits_by_change[p["change_index"]] = hits
            p["swept_blocks"] = hits
            p["stale_values"] = sorted(stale)
    if not hits_by_change:
        return []
    ids = sorted({i for h in hits_by_change.values() for i in h}, key=lambda x: int(x[1:]))
    job = Job("sweep", [Block(i, by_id[i].kind, current[i], by_id[i].path) for i in ids], [], hits_by_change)
    return [await run_job(llm, job, changes, verify)]


async def update_document(source: str, changes: list[str], llm: LLM | None = None, verify: bool = True) -> dict:
    t0 = time.perf_counter()
    llm = llm or LLM()
    blocks = split_blocks(source)
    report: dict = {"blocks": len(blocks), "words": sum(b.words for b in blocks), "changes": []}

    plan, plan_attempts, plan_errs = await plan_changes(llm, blocks, changes)
    report["plan_attempts"] = plan_attempts
    if plan is None:
        report["changes"] = [{"index": i, "change": c, "status": "failed", "errors": plan_errs} for i, c in enumerate(changes)]
        report.update(seconds=round(time.perf_counter() - t0, 2), usage=llm.usage())
        return {"updatedDocument": source, "report": report}

    # planner sometimes points at a heading when it means the content under it: add the next block too
    idx = {b.id: k for k, b in enumerate(blocks)}
    for p in plan:
        if p["scope"] == "local":
            extra = [blocks[idx[i] + 1].id for i in p["block_ids"] if blocks[idx[i]].kind == "heading"
                     and idx[i] + 1 < len(blocks) and blocks[idx[i] + 1].kind != "heading"]
            p["block_ids"] = sorted(set(p["block_ids"]) | set(extra), key=idx.get)
    _presweep(blocks, plan, changes)

    jobs = build_jobs(blocks, plan)
    results: list[JobResult] = list(await asyncio.gather(*(run_job(llm, j, changes, verify) for j in jobs)))
    failed = {i for r in results for i in r.failed}
    replacements = {i: v for r in results for i, v in r.replacements.items()}

    # a targeted change is atomic across jobs: if any of its required blocks failed, revert all of its blocks
    reverted = set()
    for p in plan:
        if p["scope"] == "local" and failed & set(p["block_ids"]):
            reverted.add(p["change_index"])
            for i in p["block_ids"] + p.get("optional_ids", []):
                replacements.pop(i, None)

    sweeps = await _sweep_stale_values(llm, blocks, [p for p in plan if p["change_index"] not in reverted],
                                       replacements, changes, verify)
    for r in sweeps:
        replacements.update(r.replacements)  # a failed sweep just leaves those blocks as they were
    results += sweeps

    # a heading whose whole section was deleted would be left orphaned: drop it too
    orphans = []
    for k, b in enumerate(blocks):
        if b.kind != "heading" or b.id in replacements:
            continue
        lvl, body = heading_level(b.text), []
        for nb in blocks[k + 1:]:
            if nb.kind == "heading" and heading_level(nb.text) <= lvl:
                break
            body.append(nb)
        if body and all(replacements.get(nb.id, "") is None for nb in body):
            replacements[b.id] = None
            orphans.append(b.id)
    report["orphan_headings_removed"] = orphans
    # a block that failed a style edit but is gone from the output anyway (orphaned heading) is not a failure
    failed = {i for i in failed if replacements.get(i, "") is not None}

    for p in sorted(plan, key=lambda p: p["change_index"]):
        ci = p["change_index"]
        mine = [r for r in results if ci in r.job.global_changes or ci in r.job.local_changes]
        errors = [e for r in mine for e in r.errors]
        if p["scope"] == "not_found":
            status = "not_found"
        elif p["scope"] == "local":
            status = "failed" if ci in reverted else "applied"
        else:
            status = "partial" if failed else "applied"
        entry = {"index": ci, "change": changes[ci], "status": status, "scope": p["scope"],
                 "blocks": p["block_ids"], "reason": p["reason"], "errors": errors}
        if p.get("optional_ids"):
            entry["presweep"] = {"optional_blocks": p["optional_ids"],
                                 "updated_blocks": [i for i in p["optional_ids"] if i in replacements]}
        if p.get("swept_blocks"):
            entry["sweep"] = {"stale_values": p["stale_values"], "checked_blocks": p["swept_blocks"],
                              "updated_blocks": [i for i in p["swept_blocks"] if any(i in r.replacements for r in sweeps)],
                              "failed_blocks": [i for i in p["swept_blocks"] if any(i in r.failed for r in sweeps)]}
        if dropped := [i for r in mine for i in r.dropped if i in p["block_ids"] + p.get("optional_ids", [])]:
            entry["dropped_blocks"] = dropped
        if p["scope"] == "global" and failed:
            entry["unchanged_blocks"] = sorted(failed, key=lambda x: int(x[1:]))
        report["changes"].append(entry)

    report["jobs"] = [{"name": r.job.name, "blocks": len(r.job.blocks), "attempts": r.attempts,
                       "failed_blocks": r.failed, "dropped_blocks": r.dropped, "changed_blocks": len(r.replacements)} for r in results]
    report.update(seconds=round(time.perf_counter() - t0, 2), usage=llm.usage())
    return {"updatedDocument": join_blocks(blocks, replacements), "report": report}
