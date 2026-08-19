"""Minimal chat-completions client.

Deliberately not an SDK. The project already has a rate-limited HTTP client with backoff
(`data_pipeline.extraction.http`), and every other external call in this codebase goes
through it — adding a second HTTP stack for one endpoint would mean two retry policies, two
timeout settings, and two places to look when a call hangs.

Two models, because the phases have different economics:

  ROUTER_MODEL     one short sentence in, small JSON out, on every query
  SYNTHESIS_MODEL  the whole retrieved context in, an analyst answer out

The plan calls this out explicitly: routing is cheap, synthesis is not. Splitting them is
what keeps the per-query cost dominated by the answer rather than the dispatch.
"""

from __future__ import annotations

import json
import os
from typing import Any

from config.settings import load_dotenv
from data_pipeline.extraction.http import HttpClient, TokenBucket

CHAT_URL = "https://api.openai.com/v1/chat/completions"

ROUTER_MODEL = os.getenv("ROUTER_MODEL", "gpt-4o-mini")
SYNTHESIS_MODEL = os.getenv("SYNTHESIS_MODEL", "gpt-4o-mini")

_http: HttpClient | None = None
_usage = {"calls": 0, "tokens": 0}


def usage() -> dict[str, int]:
    """Cumulative token spend for this process. LLM cost is per-QUERY, unlike embeddings,
    so it is worth being able to see it without reading a provider dashboard."""
    return dict(_usage)


def _client() -> HttpClient:
    global _http
    if _http is None:
        _http = HttpClient(TokenBucket(rate_per_minute=200), timeout=120)
    return _http


def complete_json(prompt: str, model: str, *, temperature: float = 0.0) -> dict[str, Any]:
    """One call, JSON object back.

    `response_format=json_object` is not decoration: without it a model asked for JSON
    intermittently wraps it in prose or a markdown fence, and the parse fails on maybe one
    call in fifty — which is exactly the failure rate that survives manual testing and
    breaks in use.
    """
    load_dotenv()
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set — see docs/api-keys.md")

    payload = _client().request_json(
        "POST",
        CHAT_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    _usage["calls"] += 1
    _usage["tokens"] += payload.get("usage", {}).get("total_tokens", 0)
    return json.loads(payload["choices"][0]["message"]["content"])
