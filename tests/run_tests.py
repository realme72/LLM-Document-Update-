"""Test suite.  python tests/run_tests.py [--offline]

Live tests hit the OpenAI API on samples/nimbus_guide.pdf (~10.5k words).
Offline tests use a scripted fake LLM to inject failures deterministically.
"""
import asyncio
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from docupdate.blocks import split_blocks  # noqa: E402
from docupdate.llm import LLM  # noqa: E402
from docupdate.pdf_io import pdf_to_markdown  # noqa: E402
from docupdate.pipeline import update_document  # noqa: E402

PRICING = "Update the pricing section to reflect a 10% increase across all tiers"
LEGACY = "Remove the paragraph about the legacy API"
FORMAL = "Make the tone throughout more formal"
CONTRACTIONS = re.compile(r"\b\w+'(ll|re|ve|t|s|d)\b|\bgonna\b|\bkinda\b|\bsuper\b", re.I)

SOURCE = pdf_to_markdown("samples/nimbus_guide.pdf")
results = []


def record(name, res, checks):
    r, u = res["report"], res["report"]["usage"]
    if u["prompt_tokens"]:  # live test: keep output for inspection
        slug = re.sub(r"\W+", "_", name).strip("_")
        os.makedirs("out/tests", exist_ok=True)
        open(f"out/tests/{slug}.md", "w").write(res["updatedDocument"])
        json.dump(r, open(f"out/tests/{slug}.report.json", "w"), indent=2)
    ok = all(v for v in checks.values())
    results.append({"test": name, "pass": ok, "seconds": r["seconds"], "calls": u["calls"],
                    "in_tokens": u["prompt_tokens"], "out_tokens": u["completion_tokens"],
                    "statuses": [c["status"] for c in r["changes"]],
                    "failed_checks": [k for k, v in checks.items() if not v]})
    print(f"{'PASS' if ok else 'FAIL'}  {name:<34} {r['seconds']:>6}s  calls={u['calls']:<3} in={u['prompt_tokens']:<6} "
          f"out={u['completion_tokens']:<6} {[c['status'] for c in r['changes']]}"
          + (f"  failed: {[k for k, v in checks.items() if not v]}" if not ok else ""))


def prices_ok(doc):
    return all(p in doc for p in ("$31.90", "$86.90", "$273.90")) and not re.search(r"\$(29|79|249)\b", doc)


# ---------------------------------------------------------------- live


async def live():
    res = await update_document(SOURCE, [PRICING, LEGACY, FORMAL])
    d = res["updatedDocument"]
    os.makedirs("out/tests", exist_ok=True)
    open("out/tests/spec_example.md", "w").write(d)
    json.dump(res["report"], open("out/tests/spec_example.report.json", "w"), indent=2)
    record("spec example (3 changes)", res, {
        "all applied": all(c["status"] == "applied" for c in res["report"]["changes"]),
        "prices +10% everywhere (table, FAQ, summary)": prices_ok(d),
        "legacy paragraph gone": "legacy v1 API" not in d and "XML responses" not in d,
        "v2 section kept": "600 requests per minute" in d,
        "contractions mostly gone": len(CONTRACTIONS.findall(d)) < 0.1 * len(CONTRACTIONS.findall(SOURCE)),
        "headings preserved (minus emptied section)": sum(b.kind == "heading" for b in split_blocks(d))
        == sum(b.kind == "heading" for b in split_blocks(SOURCE)) - len(res["report"]["orphan_headings_removed"]),
        "v2/webhooks headings kept": "API v2" in d and "Webhooks" in d,
        "links preserved": set(re.findall(r"https?://\S+", d)) >= set(re.findall(r"https?://\S+", SOURCE)) - {"https://docs.nimbus.example/api/migrate."},
    })

    res = await update_document(SOURCE, [PRICING, LEGACY])
    d = res["updatedDocument"]
    changed = sum(a != b for a, b in zip(SOURCE.split("\n\n"), d.split("\n\n")))
    record("targeted only (no tone change)", res, {
        "applied": all(c["status"] == "applied" for c in res["report"]["changes"]),
        "prices ok": prices_ok(d), "legacy gone": "legacy v1 API" not in d,
        "tone untouched": "kinda wild" in d,
        "output tokens << doc": res["report"]["usage"]["completion_tokens"] < 2000,
    })

    res = await update_document(SOURCE, ["Update the section about our blockchain integration to mention Solana support"])
    record("change with no target", res, {
        "not_found": res["report"]["changes"][0]["status"] == "not_found",
        "doc unchanged": res["updatedDocument"].strip() == "\n\n".join(b.text for b in split_blocks(SOURCE)).strip(),
    })

    res = await update_document(SOURCE, ["Rename the 'Pro' plan to 'Growth' everywhere it is mentioned"])
    d = res["updatedDocument"]
    record("rename across document", res, {
        "applied": res["report"]["changes"][0]["status"] == "applied",
        "no 'Pro plan' left": not re.search(r"\bPro\b(?! tip)", d),
        "Growth present": d.count("Growth") >= 3,
    })


# ---------------------------------------------------------------- offline (scripted fake LLM)


class FakeLLM(LLM):
    def __init__(self, script):
        self.model, self.calls, self.script = "fake", [], script
        self.sem = asyncio.Semaphore(8)

    async def chat_json(self, tag, messages, schema, model=None):
        out = self.script(tag, messages[-1]["content"])
        self.calls.append({"tag": tag, "user": messages[-1]["content"], "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "seconds": 0})
        return out


def ids_in(user):
    return re.findall(r"^\[(B\d+)\]", user, re.M)


async def offline():
    blocks = split_blocks(SOURCE)
    legacy = next(b for b in blocks if "legacy v1 API" in b.text)
    heading = next(b for b in blocks if b.kind == "heading" and "Pricing" in b.text)

    # 1. LLM makes formatting mistakes on the first try -> retried with feedback -> recovered
    attempts = {"n": 0}

    def flaky(tag, user):
        if tag.startswith("plan"):
            return json.dumps({"plan": [{"change_index": 0, "scope": "local", "block_ids": [heading.id], "reason": ""}]})
        if ":verify" in tag:
            return json.dumps({"verdicts": [{"change_index": 0, "ok": True, "problem": ""}]})
        attempts["n"] += 1
        if attempts["n"] == 1:
            return "```json\n{not json"                                              # broken JSON
        if attempts["n"] == 2:
            return json.dumps({"edits": [{"id": i, "action": "replace", "text": "[B1] Pricing Plans"} for i in ids_in(user)]})  # lost '#', leaked id
        return json.dumps({"edits": [{"id": i, "action": "replace" if i == heading.id else "keep",
                                      "text": "# **Pricing Plans**" if i == heading.id else ""} for i in ids_in(user)]})

    res = await update_document(SOURCE, ["Rename the Pricing heading to Pricing Plans"], FakeLLM(flaky))
    record("offline: bad JSON, then bad format", res, {
        "applied after retries": res["report"]["changes"][0]["status"] == "applied",
        "3 editor attempts": attempts["n"] == 3,
        "result correct": "# **Pricing Plans**" in res["updatedDocument"],
    })

    # 2. LLM never produces a valid edit -> change reported failed, doc untouched, no exception
    def hopeless(tag, user):
        if tag.startswith("plan"):
            return json.dumps({"plan": [{"change_index": 0, "scope": "local", "block_ids": [legacy.id], "reason": ""},
                                        {"change_index": 1, "scope": "not_found", "block_ids": [], "reason": ""}]})
        return json.dumps({"edits": [{"id": "B99999", "action": "replace", "text": "Sure! Here is the revised text:"}]})

    res = await update_document(SOURCE, [LEGACY, "Add a section on blockchain"], FakeLLM(hopeless))
    record("offline: persistent garbage", res, {
        "failed, not crashed": res["report"]["changes"][0]["status"] == "failed",
        "doc unchanged": "legacy v1 API" in res["updatedDocument"],
        "errors reported": bool(res["report"]["changes"][0]["errors"]),
    })

    # 3. Planner returns invalid ids first -> retried
    plan_calls = {"n": 0}

    def bad_plan(tag, user):
        if tag.startswith("plan"):
            plan_calls["n"] += 1
            ids = ["B9999"] if plan_calls["n"] == 1 else [legacy.id]
            return json.dumps({"plan": [{"change_index": 0, "scope": "local", "block_ids": ids, "reason": ""}]})
        if ":verify" in tag:
            return json.dumps({"verdicts": [{"change_index": 0, "ok": True, "problem": ""}]})
        return json.dumps({"edits": [{"id": i, "action": "delete", "text": ""} for i in ids_in(user)]})

    res = await update_document(SOURCE, [LEGACY], FakeLLM(bad_plan))
    record("offline: planner hallucinated id", res, {
        "plan retried": res["report"]["plan_attempts"] == 2,
        "applied": res["report"]["changes"][0]["status"] == "applied",
        "legacy gone": "legacy v1 API" not in res["updatedDocument"],
    })

    # 4. Planner points at a heading as well as the paragraph; editor keeps stuffing content into the
    #    heading -> after retries the heading is dropped from the change, the paragraph edit still applies
    para = next(b for b in blocks if "legacy v1 API" in b.text)
    head = next(b for b in blocks if b.kind == "heading" and "Legacy API" in b.text)
    new_para = para.text.replace("60 requests per minute", "30 requests per minute")

    def heading_stuffer(tag, user):
        if tag.startswith("plan"):
            return json.dumps({"plan": [{"change_index": 0, "scope": "local", "block_ids": [head.id, para.id], "reason": ""}]})
        if ":verify" in tag:
            return json.dumps({"verdicts": [{"change_index": 0, "ok": True, "problem": ""}]})
        return json.dumps({"edits": [{"id": i, "action": "replace",
                                      "text": head.text + "\n\nRate limit is now 30/min." if i == head.id else new_para}
                                     for i in ids_in(user)]})

    res = await update_document(SOURCE, ["Lower the legacy API rate limit to 30 requests per minute"], FakeLLM(heading_stuffer))
    c = res["report"]["changes"][0]
    record("offline: heading stuffed, dropped", res, {
        "applied": c["status"] == "applied",
        "heading dropped + kept": c.get("dropped_blocks") == [head.id] and head.text in res["updatedDocument"],
        "paragraph edited": "30 requests per minute" in res["updatedDocument"],
    })

    # 5. Pre-sweep: the planner misses the FAQ, but the FAQ states the same price, so it joins the change
    #    as an optional block in the FIRST round. An optional block that keeps failing is kept unchanged
    #    and does not sink the change.
    table = next(b for b in blocks if b.kind == "table" and "$79" in b.text)
    faq = next(b for b in blocks if "How much does the Pro plan cost" in b.text)
    summary = next(b for b in blocks if "most teams land on Pro" in b.text)

    def presweep_script(tag, user):
        if tag.startswith("plan"):
            return json.dumps({"plan": [{"change_index": 0, "scope": "local", "block_ids": [table.id], "reason": ""}]})
        if ":verify" in tag:
            return json.dumps({"verdicts": [{"change_index": 0, "ok": True, "problem": ""}]})
        out = []
        for i in ids_in(user):
            b = next(x for x in blocks if x.id == i)
            if i == summary.id:   # optional block that always comes back broken
                out.append({"id": i, "action": "replace", "text": "[B1] " + b.text})
            else:
                t = b.text.replace("$29", "$31.90").replace("$79", "$86.90").replace("$249", "$273.90").replace("$8 ", "$8.80 ")
                out.append({"id": i, "action": "replace" if t != b.text else "keep", "text": t if t != b.text else ""})
        return json.dumps({"edits": out})

    fake = FakeLLM(presweep_script)
    res = await update_document(SOURCE, [PRICING], fake)
    c = res["report"]["changes"][0]
    record("offline: pre-sweep + optional block", res, {
        "applied": c["status"] == "applied",
        "FAQ joined in first round": faq.id in c.get("presweep", {}).get("optional_blocks", []),
        "FAQ updated": "Pro is $86.90 per month" in res["updatedDocument"],
        "broken optional kept unchanged": summary.text in res["updatedDocument"] and summary.id in c.get("dropped_blocks", []),
        "FAQ not re-swept (done in round 1)": not any(x["tag"].startswith("sweep") and faq.id in x.get("user", "") for x in fake.calls),
    })

    # 6. Small document: independent changes share ONE edit call, but stay independent:
    #    one change failing does not block the other.
    small = "# Plans\n\nOur Basic plan is $10/month.\n\n## Support\n\nEmail us any time.\n\n## Region\n\nWe host in the EU.\n"
    sb = split_blocks(small)
    price_b = next(b for b in sb if "$10" in b.text)
    region_b = next(b for b in sb if "EU" in b.text)

    def small_script(tag, user):
        if tag.startswith("plan"):
            return json.dumps({"plan": [{"change_index": 0, "scope": "local", "block_ids": [price_b.id], "reason": ""},
                                        {"change_index": 1, "scope": "local", "block_ids": [region_b.id], "reason": ""}]})
        if ":verify" in tag:
            return json.dumps({"verdicts": [{"change_index": 1, "ok": False, "problem": "wrong region"}]})
        return json.dumps({"edits": [{"id": i, "action": "replace",
                                      "text": "Our Basic plan is $11.00/month." if i == price_b.id else "We host in the US."}
                                     for i in ids_in(user)]})

    fake = FakeLLM(small_script)
    res = await update_document(small, ["Raise prices by 10% increase", "Move hosting to Canada"], fake)
    edit_calls = [x["tag"] for x in fake.calls if x["tag"].startswith("group") and ":verify" not in x["tag"]]
    st = [c["status"] for c in res["report"]["changes"]]
    record("offline: small doc, one call, independent", res, {
        "one edit call per round": all(t.startswith("group0#") for t in edit_calls) and len({t.split("#")[1] for t in edit_calls}) == len(edit_calls),
        "price change applied": st[0] == "applied" and "$11.00" in res["updatedDocument"],
        "rejected change failed alone": st[1] == "failed" and "We host in the EU." in res["updatedDocument"],
    })

    # 7. Reviewer skipped when code proves a percentage change (exact scaling, nothing else touched)
    def proven_script(tag, user):
        if tag.startswith("plan"):
            return json.dumps({"plan": [{"change_index": 0, "scope": "local", "block_ids": [price_b.id], "reason": ""}]})
        if ":verify" in tag:
            raise AssertionError("reviewer should not be called")
        return json.dumps({"edits": [{"id": i, "action": "replace", "text": "Our Basic plan is $11.00/month."} for i in ids_in(user)]})

    fake = FakeLLM(proven_script)
    res = await update_document(small, ["Apply a 10% increase to all prices"], fake)
    record("offline: proven change skips reviewer", res, {
        "applied": res["report"]["changes"][0]["status"] == "applied",
        "no reviewer call": not any(":verify" in x["tag"] for x in fake.calls),
    })

    # 8. OpenAI keeps erroring (outage, deprecated model): real LLM wrapper, client always raises ->
    #    every change reported failed, original returned, no exception
    import openai

    async def always_down(**kw):
        raise openai.OpenAIError("connection reset (simulated outage)")

    down = LLM()
    down.client.chat.completions.create = always_down
    res = await update_document(SOURCE, [LEGACY], down)
    record("offline: API down, no crash", res, {
        "failed, not crashed": res["report"]["changes"][0]["status"] == "failed",
        "doc unchanged": res["updatedDocument"] == SOURCE,
        "errors reported": bool(res["report"]["usage"]["api_errors"]),
    })

    # 9. KNOWN BLIND SPOT: a style edit that silently flips meaning passes every check.
    flipped = ("The legacy v1 API remains fully supported, and new integrations may be built on it with confidence. "
               "It uses XML responses, does not support pagination, and is limited to 60 requests per minute. "
               "We are planning to retire it in 2027; customers on v1 may migrate to v2 at their convenience. "
               "Our migration guide is available at https://docs.nimbus.example/api/migrate.")

    def meaning_flip(tag, user):
        if tag.startswith("plan"):
            return json.dumps({"plan": [{"change_index": 0, "scope": "global", "block_ids": [], "reason": ""}]})
        return json.dumps({"edits": [{"id": i, "action": "replace" if i == legacy.id else "keep",
                                      "text": flipped if i == legacy.id else ""} for i in ids_in(user)]})

    res = await update_document(SOURCE, [FORMAL], FakeLLM(meaning_flip))
    record("offline: KNOWN FALSE PASS (meaning flip)", res, {
        "passes all checks (this is the bug)": res["report"]["changes"][0]["status"] == "applied",
        "wrong text applied": "may be built on it" in res["updatedDocument"],
    })
    print("\n   before:", legacy.text[:150], "...\n   after: ", flipped[:150], "...")


if __name__ == "__main__":
    asyncio.run(offline())
    if "--offline" not in sys.argv:
        asyncio.run(live())
    os.makedirs("out/tests", exist_ok=True)
    json.dump(results, open("out/tests/results.json", "w"), indent=2)
    print(f"\n{sum(r['pass'] for r in results)}/{len(results)} passed")
