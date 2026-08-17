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

# Cosine distance: 0 is identical, 2 is opposite.
#
# These two numbers were fitted against the 24-question eval set, not guessed. Neither
# works alone — measured across all 24:
#
#   distance only (0.48)      blocks 3/5 negatives, loses 0 positives
#   gap only (0.040)          blocks 5/5 negatives, loses 7 positives
#   both (0.48 + 0.025)       blocks 5/5 negatives, loses 1 positive
#
# The gap is what the absolute distance misses. An unanswerable question produces a FLAT
# distance profile — everything is equally mediocre, so nothing stands out. An answerable
# one has a clear winner. Measured mean gaps: lookup 0.077, cross_source 0.050,
# thematic 0.043, negative 0.026.
#
# Fitted on 24 questions, which is a small sample. Re-validate as the eval set grows; these
# are tuned constants, not discovered properties of the embedding space.
DEFAULT_MAX_DISTANCE = 0.48
DEFAULT_MIN_GAP = 0.025

# The gap must be measured over a FIXED window, not over however many rows the query
# happened to fetch. A longer tail raises the mean and so widens the gap for free: the
# threshold was fitted over 10 results, and applying it over the 20 that diversity
# over-fetches let two negatives through that the fit said it would block. The window makes
# the signal independent of `k` and FETCH_MULTIPLIER.
GAP_WINDOW = 10

# Diversity: how many chunks any single document may contribute, and how much to over-fetch
# to find enough distinct documents. Without this, top-5 chunks averaged 2.0 distinct
# documents — three of five slots wasted on near-duplicates from a document already
# represented, so a question expecting four documents could not succeed at k=5.
DEFAULT_MAX_PER_DOCUMENT = 1
FETCH_MULTIPLIER = 4


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


def looks_unanswerable(results: list[SearchResult], min_gap: float) -> bool:
    """True when the distance profile is flat — nothing stands out from the pack.

    Vector search always returns k results; something is always closest. On a question the
    corpus cannot answer, those k are uniformly mediocre. That flatness is a signal the
    absolute distance throws away.
    """
    window = results[:GAP_WINDOW]
    if len(window) < 2:
        return False
    best = window[0].distance
    mean = sum(r.distance for r in window) / len(window)
    return (mean - best) < min_gap


def _cap_per_document(results: list[SearchResult], k: int, max_per_doc: int) -> list[SearchResult]:
    """Keep at most `max_per_doc` chunks from any one document, preserving rank order."""
    seen: dict[tuple[str, str], int] = {}
    kept: list[SearchResult] = []
    for r in results:
        key = (r.source, r.document_id)
        if seen.get(key, 0) >= max_per_doc:
            continue
        seen[key] = seen.get(key, 0) + 1
        kept.append(r)
        if len(kept) >= k:
            break
    return kept


def search(
    query: str,
    k: int = 5,
    max_distance: float | None = DEFAULT_MAX_DISTANCE,
    min_gap: float | None = DEFAULT_MIN_GAP,
    max_per_document: int | None = DEFAULT_MAX_PER_DOCUMENT,
    protocol: str | None = None,
    source: str | None = None,
    as_of: datetime | None = None,
    conn: psycopg.Connection | None = None,
) -> list[SearchResult]:
    """Return up to `k` chunks ordered by cosine distance.

    `max_distance=None` and `min_gap=None` disable the relevance cutoffs, which is what
    recall measurement wants — it should score the ranking, not the cutoff. Application
    code should leave both on.

    `max_per_document` caps how many chunks one document may contribute. Measured on the
    eval set: capping at 1 lifts micro recall from 0.67 to 0.78 and lookup from 0.88 to
    0.96, with no extra tokens handed to the analyst. Set it to None for raw top-k.

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

    # Over-fetch when diversifying: the top k chunks may come from only two documents, so
    # finding k distinct ones means looking deeper.
    fetch = k * FETCH_MULTIPLIER if max_per_document else k

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
        rows = conn.execute(sql, [vec, *params, vec, fetch]).fetchall()
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
    # Flatness is judged on the raw ranked set, before any filtering — the shape of the
    # whole profile is the signal, and trimming it first would destroy that shape.
    if min_gap is not None and looks_unanswerable(results, min_gap):
        return []

    if max_per_document:
        results = _cap_per_document(results, k, max_per_document)
    else:
        results = results[:k]

    if max_distance is not None:
        results = [r for r in results if r.distance <= max_distance]
    return results
