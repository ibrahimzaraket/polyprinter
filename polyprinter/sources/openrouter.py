"""Client for openrouter.ai — the only LLM call surface in this project
(mandate issuance, Phase 3). Verified live 2026-08-08 against the
configured model (see config.yaml `mandate.model`), not inferred from
OpenRouter's docs alone:

- The configured model is a *reasoning* model. Its reasoning tokens are
  emitted as part of the completion, BEFORE the actual answer, and count
  against `max_tokens` — a naive small `max_tokens` (e.g. 200) burns the
  entire budget on the reasoning trace and truncates the real JSON
  mid-string (`finish_reason: "length"`, invalid JSON). Fixed two ways:
  `reasoning: {"effort": "low"}` cuts reasoning-token spend roughly 30x
  in testing, and `max_tokens` is set generously (see ISSUE_MAX_TOKENS)
  so a short prose `reasoning` field never gets cut off either.
- `response_format: {"type": "json_schema", "json_schema": {"strict":
  true, ...}}` works against this model/provider (DeepInfra) — used here
  instead of the looser `json_object` mode for a second layer of
  validation on top of mandate/schema.py's pydantic model.
- The response's `usage.cost` field is the actual billed USD cost for
  that call — used directly for budget tracking rather than
  recomputing from token counts x published pricing (fewer places for
  that number to drift from what OpenRouter actually charged).

Every call persists its raw response before parsing (sources/raw_store.py
— the same structural rule as the Polymarket clients), so a shape change
here shows up as a parse error against stored data, not a silently wrong
mandate.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx

from polyprinter.sources.raw_store import store_raw
from polyprinter.sources.retry import with_retry

BASE_URL = "https://openrouter.ai/api/v1"
SOURCE = "openrouter"


class OpenRouterClient:
    def __init__(self, conn: sqlite3.Connection, *, api_key: str, model: str, timeout: float = 60.0):
        self.conn = conn
        self.model = model
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenRouterClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def complete_json(
        self,
        prompt: str,
        *,
        json_schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
        reasoning_effort: str = "low",
    ) -> dict[str, Any]:
        """One chat completion, constrained to `json_schema` via strict
        structured output. Returns
        {"content": <raw JSON string>, "prompt_tokens": int,
         "completion_tokens": int, "reasoning_tokens": int,
         "cost_usd": float, "finish_reason": str}.

        Does NOT parse/validate `content` as the target schema — that's
        mandate/schema.py's job (hard validation, invariant-worthy on its
        own). This layer only guarantees it's a raw response worth
        looking at.
        """
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
            },
            "reasoning": {"effort": reasoning_effort},
            "max_tokens": max_tokens,
        }
        resp = with_retry(lambda: self._client.post("/chat/completions", json=body))
        store_raw(
            self.conn,
            source=SOURCE,
            url=str(resp.request.url),
            status=resp.status_code,
            body=resp.text,
        )
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        usage = data.get("usage", {})
        reasoning_tokens = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
        # `content` came back Python None at least once live (2026-08-08,
        # provider-side, cause not fully pinned down) — coerced to "" here
        # so every caller can rely on this always being a str, never None.
        # An empty string still fails downstream JSON validation loudly
        # (as it should); it just does so as a normal parse failure
        # instead of crashing the whole batch on a NOT NULL insert.
        content = choice["message"].get("content") or ""
        return {
            "content": content,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": reasoning_tokens,
            "cost_usd": usage.get("cost"),
            "finish_reason": choice.get("finish_reason"),
        }
