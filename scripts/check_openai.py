#!/usr/bin/env python3
"""Verify the OpenAI credential produces what this project's schema expects.

Deliberately NOT part of `make verify`: the acceptance harness must run without any
credential, so a contributor can build and test the stack for free. This is a separate,
opt-in check you run once after adding the key.

It fails loudly at the moment the credential is wrong rather than three-quarters of the
way through a Phase 4 backfill. Four things are checked, and the last two are the ones
that would otherwise surface as confusing pgvector errors much later:

  1. the key is present and authenticates
  2. the embedding model is reachable
  3. `dimensions` is honoured — the model returns 3072 by default, which cannot go in
     VECTOR(1536) and cannot be HNSW-indexed at all (pgvector caps indexing at 2000)
  4. the returned vector is unit-normalised — cosine distance assumes it, and a
     Matryoshka-truncated vector that came back un-normalised would need renormalising
     before insert
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import load_dotenv  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ENDPOINT = "https://api.openai.com/v1/embeddings"
PROBE_TEXT = "Aave risk parameter change: LTV increase for wstETH collateral"

GREEN, RED, DIM, RESET = "\033[0;32m", "\033[0;31m", "\033[0;90m", "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}  ok  {msg}{RESET}", flush=True)


def fail(msg: str, hint: str = "") -> None:
    # Flush stdout first, or the preceding ok() lines interleave after this one when
    # output is piped and the two streams buffer independently.
    sys.stdout.flush()
    print(f"{RED} FAIL {msg}{RESET}", file=sys.stderr, flush=True)
    if hint:
        print(f"{DIM}      {hint}{RESET}", file=sys.stderr, flush=True)


def main() -> int:
    load_dotenv()

    model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
    expected_dims = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    print(f"{DIM}model={model} dimensions={expected_dims}{RESET}", flush=True)

    # 1 — key present
    if not api_key:
        fail(
            "OPENAI_API_KEY is not set",
            "Add it to .env (never .env.example). See docs/api-keys.md.",
        )
        return 1
    if not api_key.startswith("sk-"):
        fail(
            f"OPENAI_API_KEY does not look like a key (starts {api_key[:3]!r})",
            "Expected an sk-... value. Check for a stray quote or a copied placeholder.",
        )
        return 1
    ok(f"key present ({api_key[:7]}…{api_key[-4:]})")

    # 2 — reachable and authenticated
    try:
        response = requests.post(
            ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "input": PROBE_TEXT, "dimensions": expected_dims},
            timeout=30,
        )
    except requests.RequestException as exc:
        fail(f"could not reach {ENDPOINT}", str(exc))
        return 1

    if response.status_code != 200:
        # Parse defensively rather than sniffing content-type — the header varies and a
        # missed parse dumps 300 characters of raw JSON where a one-line message belongs.
        try:
            body = response.json().get("error", {}) or {}
        except ValueError:
            body = {}
        code, message = body.get("code"), body.get("message") or response.text[:200]
        hints = {
            "invalid_api_key": "The key is wrong or was revoked. Generate a new one.",
            "insufficient_quota": (
                "The key is valid but the account has no credit. Add a payment method "
                "under Settings > Billing — this is the most common first failure."
            ),
            "model_not_found": f"{model!r} is not available to this key. Check the model string.",
            "rate_limit_exceeded": "Rate limited, not a credential problem. Retry shortly.",
        }
        fail(f"HTTP {response.status_code}: {message}", hints.get(code, ""))
        return 1
    ok("authenticated and model reachable")

    payload = response.json()
    embedding = payload["data"][0]["embedding"]

    # 3 — dimensions honoured
    if len(embedding) != expected_dims:
        fail(
            f"got {len(embedding)} dimensions, expected {expected_dims}",
            "The `dimensions` parameter was not applied. A 3072-wide vector cannot go in "
            "VECTOR(1536), and pgvector refuses to HNSW-index above 2000 dimensions.",
        )
        return 1
    ok(f"returned exactly {expected_dims} dimensions")

    # 4 — unit-normalised, so cosine distance behaves
    norm = math.sqrt(sum(x * x for x in embedding))
    if abs(norm - 1.0) > 1e-3:
        fail(
            f"vector is not unit-normalised (L2 norm = {norm:.6f})",
            "Cosine distance assumes unit vectors. Renormalise before inserting into "
            "pgvector, or similarity scores will be wrong.",
        )
        return 1
    ok(f"unit-normalised (L2 norm = {norm:.6f})")

    usage = payload.get("usage", {})
    tokens = usage.get("total_tokens", "?")
    print(
        f"\n{GREEN}PASS{RESET} — credential is ready for Phase 4."
        f"\n{DIM}probe used {tokens} tokens. Embeddings bill per token, not per dimension:"
        f"\n{expected_dims} dimensions costs exactly what 3072 would.{RESET}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
