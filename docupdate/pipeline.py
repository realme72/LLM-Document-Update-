"""Plan -> edit -> validate -> verify -> apply.

1. PLAN   (1 call, sees whole doc)  : map each change to block IDs, or 'global', or 'not_found'.
2. EDIT   (N parallel calls)        : each job sees only its blocks and returns per-block
                                      replace/delete/keep. Output ~ size of the edit.
3. CHECK  (deterministic)           : schema, ids, Markdown structure, numbers/links preserved
                                      under style-only edits, length sanity.
4. VERIFY (1 small call per job with targeted changes): LLM judge on before/after of the
                                      touched blocks only.
5. RETRY  : only the blocks that failed are re-requested, with the errors as feedback
            (max MAX_RETRIES). Style-only blocks are accepted one by one; blocks of a
            targeted change are accepted all-or-nothing, across jobs. Anything still failing
            keeps its original text and is reported. One bad output never sinks the document.
6. SWEEP  : values a targeted change replaced (e.g. "$79") that still appear elsewhere get a
            small follow-up edit - this catches the planner missing a block.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from .blocks import Block, heading_level, join_blocks, render_for_llm, split_blocks
from .llm import LLM
from .validators import changed_values, check_percent_change, percent_hint, parse_json, validate_block_edit, validate_plan

MAX_RETRIES = 2
GLOBAL_CHUNK_WORDS = 700

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

    def is_local(self, block_id: str) -> bool:
        return any(block_id in ids for ids in self.local_changes.values())


@dataclass
class JobResult:
    job: Job
    attempts: int
    replacements: dict[str, str | None]  # accepted edits only (id -> text, None = delete)
    failed: list[str]                    # block ids that never produced an acceptable edit
    errors: list[str]
    dropped: list[str] = field(default_factory=list)  # headings dropped from a targeted change, kept unchanged


# ------------------------------------------------------------------ helpers


def _instructions(job: Job, changes: list[str], ids: set[str]) -> str:
    by_id = {b.id: b for b in job.blocks}
    lines = [f"- (#{i}) [all blocks] {changes[i]}" for i in job.global_changes]
    for i, bids in job.local_changes.items():
        if mine := [b for b in bids if b in ids]:
            hint = percent_hint(changes[i], [by_id[b].text for b in mine])
            lines.append(f"- (#{i}) [only {', '.join(mine)}] {changes[i]}{hint}")
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


def build_jobs(blocks: list[Block], plan: list[dict]) -> list[Job]:
    global_idx = [p["change_index"] for p in plan if p["scope"] == "global"]
    local = {p["change_index"]: list(dict.fromkeys(p["block_ids"])) for p in plan if p["scope"] == "local"}

    if global_idx:
        # Style change touches everything: fan out over fixed-size chunks in parallel.
        # Targeted changes ride along with whichever chunk holds their blocks.
        jobs = []
        for k, chunk in enumerate(_chunk(blocks, GLOBAL_CHUNK_WORDS)):
            ids = {b.id for b in chunk}
            lc = {ci: [b for b in bids if b in ids] for ci, bids in local.items()}
            jobs.append(Job(f"chunk{k}", chunk, global_idx, {ci: v for ci, v in lc.items() if v}))
        return jobs

    # Only targeted changes: union changes that share blocks so no block is edited twice.
    by_id = {b.id: b for b in blocks}
    groups: list[tuple[set[int], set[str]]] = []
    for ci, bids in local.items():
        merged_c, merged_b = {ci}, set(bids)
        for g in [g for g in groups if g[1] & merged_b]:
            groups.remove(g)
            merged_c |= g[0]
            merged_b |= g[1]
        groups.append((merged_c, merged_b))
    order = {b.id: i for i, b in enumerate(blocks)}
    return [Job(f"group{k}", [by_id[b] for b in sorted(bs, key=order.get)], [], {ci: local[ci] for ci in sorted(cs)})
            for k, (cs, bs) in enumerate(groups)]


# ------------------------------------------------------------------ stages


async def plan_changes(llm: LLM, blocks: list[Block], changes: list[str]) -> tuple[list[dict] | None, int, list[str]]:
    user = "DOCUMENT:\n" + render_for_llm(blocks) + "\n\nCHANGES:\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(changes))
    ids = {b.id for b in blocks}
    messages = [{"role": "system", "content": PLANNER_SYSTEM}, {"role": "user", "content": user}]
    errs: list[str] = []
    for attempt in range(1, MAX_RETRIES + 2):
        raw = await llm.chat_json(f"plan#{attempt}", messages, PLAN_SCHEMA)
        parsed, errs = parse_json(raw)
        if parsed is not None:
            errs = validate_plan(parsed, len(changes), ids)
        if not errs:
            return parsed["plan"], attempt, []
        # document prefix is unchanged, so the retry hits OpenAI's prompt cache
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": "Your output had these problems:\n- " + "\n- ".join(errs) + "\nReturn the complete corrected JSON."}]
    return None, MAX_RETRIES + 1, errs


async def run_job(llm: LLM, job: Job, changes: list[str], verify: bool) -> JobResult:
    by_id = {b.id: b for b in job.blocks}
    accepted: dict[str, str | None] = {}
    pending = list(by_id)
    feedback: list[str] = []
    dropped: list[str] = []
    attempt = 0
    while pending and attempt <= MAX_RETRIES:
        attempt += 1
        user = "INSTRUCTIONS:\n" + _instructions(job, changes, set(pending)) + "\n\nBLOCKS:\n" + "\n\n".join(
            f"[{i}] (section: {by_id[i].path or '-'})\n{by_id[i].text}" for i in pending)
        if feedback:
            user += "\n\nA previous attempt at these blocks was rejected. Fix these problems:\n- " + "\n- ".join(feedback)
        raw = await llm.chat_json(f"{job.name}#{attempt}", [{"role": "system", "content": EDITOR_SYSTEM},
                                                            {"role": "user", "content": user}], EDIT_SCHEMA)
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
        # blocks of targeted changes: all-or-nothing, then the reviewer
        loc = [i for i in pending if job.is_local(i)]
        # Last attempt: the planner sometimes points at a heading when it means the paragraph under it,
        # and the editor then keeps pushing content into the heading. If headings are the only blocks
        # still failing, drop them from the change (kept unchanged) and judge the rest on its own.
        if attempt > MAX_RETRIES:
            stuck = [i for i in loc if block_errs[i]]
            if stuck and all(by_id[i].kind == "heading" for i in stuck):
                for i in stuck:
                    accepted[i] = by_id[i].text
                    block_errs[i] = []
                    dropped.append(i)
                loc = [i for i in loc if i not in stuck]
        if loc and not any(block_errs[i] for i in loc):
            cand = {i: value(i) for i in loc}
            problems = [e for ci, bids in job.local_changes.items() for i in bids if i in cand
                        for e in check_percent_change(changes[ci], by_id[i], cand[i])]
            if not problems and verify:
                problems = await _verify(llm, job, changes, cand, by_id)
            if problems:
                feedback += problems
            else:
                accepted.update(cand)
        feedback += [e for i in pending for e in block_errs[i]]
        pending = [i for i in by_id if i not in accepted]

    changed = {i: v for i, v in accepted.items() if v is None or v.strip() != by_id[i].text.strip()}
    return JobResult(job, attempt, changed, pending, feedback if pending else [], dropped)


async def _verify(llm: LLM, job: Job, changes: list[str], cand: dict[str, str | None], by_id: dict[str, Block]) -> list[str]:
    pairs = [f"[{i}] (section: {by_id[i].path or '-'})\nBEFORE:\n{by_id[i].text}\nAFTER:\n{'<<DELETED>>' if v is None else v}"
             for i, v in cand.items()]
    user = "INSTRUCTIONS:\n" + _instructions(job, changes, set(cand)) + "\n\nEDITED BLOCKS:\n" + "\n\n".join(pairs)
    raw = await llm.chat_json(f"{job.name}:verify", [{"role": "system", "content": VERIFIER_SYSTEM},
                                                     {"role": "user", "content": user}], VERIFY_SCHEMA)
    parsed, _ = parse_json(raw)
    if parsed is None:
        return []  # unreadable review is not evidence of a bad edit; deterministic checks already passed
    return [f"reviewer rejected change #{v['change_index']}: {v['problem']}" for v in parsed["verdicts"]
            if not v["ok"] and v["change_index"] in job.local_changes]


async def _sweep_stale_values(llm, blocks, plan, replacements, changes, verify) -> list[JobResult]:
    """A targeted change replaced e.g. '$79' -> '$86.90'. If '$79' still appears in a block the
    planner did not select, give that block a follow-up edit (it may answer 'keep')."""
    by_id = {b.id: b for b in blocks}
    current = {b.id: replacements.get(b.id, b.text) for b in blocks}
    jobs = []
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
        hits = [i for i, t in current.items() if t and i not in p["block_ids"]
                and any(re.search(rf"(?<![\w$]){re.escape(v)}(?![\w.]\d|\w)", t) for v in stale)]
        if hits:
            ci = p["change_index"]
            cur_blocks = [Block(i, by_id[i].kind, current[i], by_id[i].path) for i in hits]
            jobs.append(Job(f"sweep{ci}", cur_blocks, [], {ci: hits}))
            p["swept_blocks"] = hits
            p["stale_values"] = sorted(stale)
    return list(await asyncio.gather(*(run_job(llm, j, changes, verify) for j in jobs)))


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
    for p in plan:
        if p["scope"] == "local":
            idx = {b.id: k for k, b in enumerate(blocks)}
            extra = [blocks[idx[i] + 1].id for i in p["block_ids"] if blocks[idx[i]].kind == "heading"
                     and idx[i] + 1 < len(blocks) and blocks[idx[i] + 1].kind != "heading"]
            p["block_ids"] = sorted(set(p["block_ids"]) | set(extra), key=idx.get)

    jobs = build_jobs(blocks, plan)
    results: list[JobResult] = list(await asyncio.gather(*(run_job(llm, j, changes, verify) for j in jobs)))
    failed = {i for r in results for i in r.failed}
    replacements = {i: v for r in results for i, v in r.replacements.items()}

    # a targeted change is atomic across jobs: if any of its blocks failed, revert all of them
    reverted = set()
    for p in plan:
        if p["scope"] == "local" and failed & set(p["block_ids"]):
            reverted.add(p["change_index"])
            for i in p["block_ids"]:
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
        if p.get("swept_blocks"):
            entry["sweep"] = {"stale_values": p["stale_values"], "checked_blocks": p["swept_blocks"],
                              "updated_blocks": [i for r in sweeps if ci in r.job.local_changes for i in r.replacements],
                              "failed_blocks": [i for r in sweeps if ci in r.job.local_changes for i in r.failed]}
        if dropped := [i for r in mine for i in r.dropped if i in p["block_ids"]]:
            entry["dropped_headings"] = dropped
        if p["scope"] == "global" and failed:
            entry["unchanged_blocks"] = sorted(failed, key=lambda x: int(x[1:]))
        report["changes"].append(entry)

    report["jobs"] = [{"name": r.job.name, "blocks": len(r.job.blocks), "attempts": r.attempts,
                       "failed_blocks": r.failed, "dropped_headings": r.dropped, "changed_blocks": len(r.replacements)} for r in results]
    report.update(seconds=round(time.perf_counter() - t0, 2), usage=llm.usage())
    return {"updatedDocument": join_blocks(blocks, replacements), "report": report}
