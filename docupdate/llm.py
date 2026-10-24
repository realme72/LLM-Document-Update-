"""Thin OpenAI wrapper: strict JSON-schema output, concurrency cap, token accounting."""
from __future__ import annotations

import asyncio
import os
import time

from openai import AsyncOpenAI


class LLM:
    def __init__(self, model: str | None = None, max_concurrency: int = 8):
        self.model = model or os.getenv("DOCUPDATE_MODEL", "gpt-4.1-mini")
        self.client = AsyncOpenAI(max_retries=3, timeout=120)  # transport-level retries (429/5xx/timeouts)
        self.sem = asyncio.Semaphore(max_concurrency)
        self.calls: list[dict] = []

    async def chat_json(self, tag: str, messages: list[dict], schema: dict) -> str:
        """Returns the raw JSON string; parsing/validation is the caller's job."""
        async with self.sem:
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
