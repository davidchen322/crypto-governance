"""Vector search over pgvector.

Deliberately a plain function, not an agent. The LangGraph router arrives in Phase 5; this
is the layer it will call, and it needs to be measurable on its own first — an agent
wrapped around unmeasured retrieval just makes the failure harder to locate.

Two things here exist because of the eval set's negative questions:

  * `search()` returns distances, not just rows. Without a score there is no way to express
    "nothing here is close enough".
  * `max_distance` gives that expression a default. Vector search always returns k results —
    something is always closest — so a question the corpus cannot answer still comes back
    with five confident-looking chunks unless a threshold rejects them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

import psycopg

from config.settings import Settings, load_dotenv
from data_pipeline.extraction.http import HttpClient, TokenBucket

EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"

# Cosine distance, so 0 is identical and 2 is opposite. 0.75 is a starting point chosen to
# be tuned against the eval set, not a discovered constant — the negatives measure how much
# leaks through and the positives measure what it wrongly rejects.
DEFAULT_MAX_DISTANCE = 0.75


@dataclass(frozen=True)
class SearchResult:
    source: str  # 'proposal' | 'forum'
    document_id: str
    protocol_name: str
    chunk_index: int
    heading: str | None
    text: str
    distance: float

    @property
    def similarity(self) -> float:
        return 1.0 - self.distance


_http: HttpClient | None = None


def embed_query(text: str) -> list[float]:
    """Embed a single query string with the same model the corpus was embedded with.

    Vectors from different models share no coordinate space, so a mismatch here does not
    error — it silently returns nonsense ranked by a meaningless distance.
    """
    global _http
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set — see docs/api-keys.md")

    if _http is None:
        _http = HttpClient(TokenBucket(rate_per_minute=200), timeout=60)

    payload = _http.request_json(
        "POST",
        EMBEDDINGS_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": os.getenv("EMBEDDING_MODEL", "text-embedding-3-large"),
            "input": text,
            "dimensions": int(os.getenv("EMBEDDING_DIMENSIONS", "1536")),
        },
    )
    return payload["data"][0]["embedding"]


def search(
    query: str,
    k: int = 5,
    max_distance: float | None = DEFAULT_MAX_DISTANCE,
    protocol: str | None = None,
    source: str | None = None,
    as_of: datetime | None = None,
    conn: psycopg.Connection | None = None,
) -> list[SearchResult]:
    """Return up to `k` chunks ordered by cosine distance.

    `max_distance=None` disables the threshold, which is what recall measurement wants —
    it should score the ranking, not the cutoff. Application code should leave the
    threshold on.

    `as_of` asks the question against a past state, using the validity windows carried down
    from silver. Without it, a historical question retrieves today's text and the analyst
    cites it as though it were contemporaneous.
    """
    vector = embed_query(query)
    model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")

    # embedding_model is not optional in this filter. Mixing models in one similarity
    # search compares coordinates from unrelated spaces.
    where = ["embedding_model = %s"]
    params: list = [model]
    if protocol:
        where.append("protocol_name = %s")
        params.append(protocol)
    if source:
        where.append("source = %s")
        params.append(source)
    if as_of:
        where.append("valid_from <= %s AND (valid_to > %s OR valid_to IS NULL)")
        params.extend([as_of, as_of])

    sql = f"""
        SELECT source, document_id, protocol_name, chunk_index, heading, text_chunk,
               embedding <=> %s::vector AS distance
        FROM document_embeddings
        WHERE {" AND ".join(where)}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    vec = str(vector)

    owned = conn is None
    if owned:
        conn = psycopg.connect(Settings.from_env().postgres_dsn)
    try:
        rows = conn.execute(sql, [vec, *params, vec, k]).fetchall()
    finally:
        if owned:
            conn.close()

    results = [
        SearchResult(
            source=r[0],
            document_id=r[1],
            protocol_name=r[2],
            chunk_index=r[3],
            heading=r[4],
            text=r[5],
            distance=float(r[6]),
        )
        for r in rows
    ]
    if max_distance is not None:
        results = [r for r in results if r.distance <= max_distance]
    return results
