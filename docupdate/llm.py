"""Thin OpenAI wrapper: strict JSON-schema output, concurrency caps, token accounting.

Two concurrency limits:
  per document  DOCUPDATE_CONCURRENCY         (default 32) - calls one document may have in flight;
                                                set above the chunk count so a style pass runs in one wave
  per process   DOCUPDATE_GLOBAL_CONCURRENCY  (default 64) - calls in flight across ALL documents
                                                (API server / batch), so N parallel requests can't burst
                                                N x 32 calls at the provider
"""
from __future__ import annotations

import asyncio
import os
import time

from openai import AsyncOpenAI

_global_sems: dict[int, asyncio.Semaphore] = {}


def _global_sem() -> asyncio.Semaphore:
    # semaphores belong to an event loop; the CLI/tests may start several loops in one process
    loop = asyncio.get_running_loop()
    key = id(loop)
    if key not in _global_sems:
        _global_sems.clear()
        _global_sems[key] = asyncio.Semaphore(int(os.getenv("DOCUPDATE_GLOBAL_CONCURRENCY", "64")))
    return _global_sems[key]


class LLM:
    def __init__(self, model: str | None = None, max_concurrency: int | None = None):
        self.model = model or os.getenv("DOCUPDATE_MODEL", "gpt-4.1-mini")
        self.client = AsyncOpenAI(max_retries=3, timeout=120)  # transport-level retries (429/5xx/timeouts)
        self.sem = asyncio.Semaphore(max_concurrency or int(os.getenv("DOCUPDATE_CONCURRENCY", "32")))
        self.calls: list[dict] = []

    async def chat_json(self, tag: str, messages: list[dict], schema: dict) -> str:
        """Returns the raw JSON string; parsing/validation is the caller's job."""
        async with self.sem, _global_sem():
            t0 = time.perf_counter()
            r = await self.client.chat.completions.create(
                model=self.model,
                temperature=0,
                messages=messages,
                response_format={"type": "json_schema", "json_schema": {"name": "out", "schema": schema, "strict": True}},
            )
        u = r.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        self.calls.append({"tag": tag, "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                           "cached_tokens": cached, "seconds": round(time.perf_counter() - t0, 2)})
        return r.choices[0].message.content or ""

    def usage(self) -> dict:
        return {
            "model": self.model,
            "calls": len(self.calls),
            "prompt_tokens": sum(c["prompt_tokens"] for c in self.calls),
            "completion_tokens": sum(c["completion_tokens"] for c in self.calls),
            "cached_tokens": sum(c["cached_tokens"] for c in self.calls),
            "per_call": self.calls,
        }
