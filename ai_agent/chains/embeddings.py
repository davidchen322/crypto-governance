"""Embed silver documents into pgvector.

    make embed              # only what is missing
    make embed ARGS=--dry-run   # cost and count, no API calls

Idempotency is the design constraint, because this is the first component that costs money.
The loader computes every chunk, asks Postgres which `(source_content_hash, chunk_index,
embedding_model)` triples already exist, and embeds only the remainder. A re-run over
unchanged silver makes zero API calls and costs nothing.

That is also why Phase 3 splits its hashes. `content_hash` covers title and body only, so a
proposal whose vote tally moved is a new silver *row* but the same text — and this loader
correctly declines to re-embed it. Had versioning and text identity shared one hash, every
vote landing on an active proposal would have re-billed its embeddings.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

import psycopg

from ai_agent.chains.chunking import CHUNK_SCHEME, chunk_forum_post, chunk_proposal
from ai_agent.chains.trino_client import query as trino_query
from config.corpus_filters import (
    MIN_BODY_TOKENS,
    is_boilerplate_topic,
    is_substantive_chunk,
)
from config.protocols import BY_NAME
from config.settings import Settings, load_dotenv
from data_pipeline.extraction.http import HttpClient, TokenBucket

EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"

# OpenAI accepts up to 2048 inputs per request, but the practical limit is tokens. A batch
# is capped on whichever bound is hit first — an oversized batch is rejected wholesale,
# which on a long run means losing the work of every input in it.
MAX_BATCH_INPUTS = 128
MAX_BATCH_TOKENS = 100_000


@dataclass
class PendingChunk:
    source: str
    document_id: str
    protocol_name: str
    source_content_hash: str
    chunk_type: str
    chunk_index: int
    heading: str | None
    text: str
    tokens: int
    valid_from: str
    valid_to: str | None
    # Citation metadata only — never used for retrieval. `document_date` is the governance
    # date, not `valid_from`, which is merely when the pipeline observed the row.
    title: str | None = None
    document_date: str | None = None


def load_silver_chunks() -> list[PendingChunk]:
    """Chunk every current silver document. Pure — no database writes, no API calls."""
    pending: list[PendingChunk] = []

    proposals = trino_query("""
        SELECT proposal_id, protocol_name, content_hash, title, body,
               cast(proposal_created AS varchar) AS document_date,
               cast(valid_from AS varchar) AS valid_from,
               cast(valid_to AS varchar) AS valid_to
        FROM iceberg.silver.proposal_versions
        WHERE is_current
    """)
    # Curation counters, reported rather than silently applied. A filter that quietly drops
    # source data is indistinguishable from a bug in the extractor.
    dropped_boilerplate = 0
    dropped_runt = 0

    for row in proposals:
        label = BY_NAME[row["protocol_name"]].label
        chunks = chunk_proposal(row["title"], row["body"], protocol=label)
        for chunk in chunks:
            # A proposal that reduces to one chunk is exempt: it is a governance act with a
            # vote attached and nothing else in the corpus stands in for it.
            if not is_substantive_chunk(
                chunk.body_tokens, is_indivisible_document=len(chunks) == 1
            ):
                dropped_runt += 1
                continue
            pending.append(
                PendingChunk(
                    source="proposal",
                    document_id=row["proposal_id"],
                    protocol_name=row["protocol_name"],
                    source_content_hash=row["content_hash"],
                    chunk_type="proposal_body",
                    chunk_index=chunk.index,
                    heading=chunk.heading,
                    text=chunk.text,
                    tokens=chunk.tokens,
                    valid_from=row["valid_from"],
                    valid_to=row["valid_to"],
                    title=row["title"],
                    document_date=row["document_date"],
                )
            )

    posts = trino_query("""
        SELECT cast(topic_id AS varchar) AS topic_id, protocol_name, content_hash,
               topic_title, body_text,
               cast(post_created_at AS varchar) AS document_date,
               cast(valid_from AS varchar) AS valid_from,
               cast(valid_to AS varchar) AS valid_to
        FROM iceberg.silver.forum_posts
        WHERE is_current
    """)
    for row in posts:
        label = BY_NAME[row["protocol_name"]].label
        if is_boilerplate_topic(row["topic_title"]):
            dropped_boilerplate += 1
            continue
        chunks = chunk_forum_post(row["topic_title"], row["body_text"], protocol=label)
        for chunk in chunks:
            # No exemption for forum posts: a one-line reply is not a governance act, and its
            # topic stays retrievable through the posts that do carry substance.
            if not is_substantive_chunk(chunk.body_tokens):
                dropped_runt += 1
                continue
            pending.append(
                PendingChunk(
                    source="forum",
                    document_id=row["topic_id"],
                    protocol_name=row["protocol_name"],
                    source_content_hash=row["content_hash"],
                    chunk_type="forum_post",
                    chunk_index=chunk.index,
                    heading=chunk.heading,
                    text=chunk.text,
                    tokens=chunk.tokens,
                    valid_from=row["valid_from"],
                    valid_to=row["valid_to"],
                    title=row["topic_title"],
                    document_date=row["document_date"],
                )
            )

    if dropped_boilerplate or dropped_runt:
        print(
            f"  curation: skipped {dropped_boilerplate} boilerplate post(s), "
            f"{dropped_runt} chunk(s) under {MIN_BODY_TOKENS} body tokens"
        )
    return pending


def filter_already_embedded(
    conn: psycopg.Connection, pending: list[PendingChunk], model: str
) -> list[PendingChunk]:
    """Drop chunks already present for this model.

    Checked before embedding, not enforced by ON CONFLICT afterwards — the point is to
    avoid paying for a vector that would then be discarded.
    """
    existing = {
        (h, i)
        for h, i in conn.execute(
            "SELECT source_content_hash, chunk_index FROM document_embeddings "
            "WHERE embedding_model = %s AND chunk_scheme = %s",
            (model, CHUNK_SCHEME),
        ).fetchall()
    }
    return [c for c in pending if (c.source_content_hash, c.chunk_index) not in existing]


def batches(chunks: list[PendingChunk]):
    """Group by input count and token budget, whichever binds first."""
    batch: list[PendingChunk] = []
    tokens = 0
    for chunk in chunks:
        if batch and (len(batch) >= MAX_BATCH_INPUTS or tokens + chunk.tokens > MAX_BATCH_TOKENS):
            yield batch
            batch, tokens = [], 0
        batch.append(chunk)
        tokens += chunk.tokens
    if batch:
        yield batch


def embed_batch(http: HttpClient, api_key: str, model: str, dims: int, texts: list[str]):
    payload = http.request_json(
        "POST",
        EMBEDDINGS_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "input": texts, "dimensions": dims},
    )
    # The API returns embeddings with an `index` field; relying on list order without
    # sorting would silently mis-pair vectors with their text.
    ordered = sorted(payload["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in ordered], payload.get("usage", {}).get("total_tokens", 0)


def insert(conn: psycopg.Connection, chunks: list[PendingChunk], vectors, model: str) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO document_embeddings
                (source, document_id, protocol_name, source_content_hash, chunk_type,
                 chunk_index, heading, text_chunk, token_count, embedding_model,
                 chunk_scheme, embedding, valid_from, valid_to, title, document_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT ON CONSTRAINT document_embeddings_chunk_unique DO NOTHING
            """,
            [
                (
                    c.source,
                    c.document_id,
                    c.protocol_name,
                    c.source_content_hash,
                    c.chunk_type,
                    c.chunk_index,
                    c.heading,
                    c.text,
                    c.tokens,
                    model,
                    CHUNK_SCHEME,
                    str(vec),
                    c.valid_from,
                    c.valid_to or None,
                    c.title,
                    c.document_date or None,
                )
                for c, vec in zip(chunks, vectors, strict=True)
            ],
        )
        return cur.rowcount


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Embed silver documents into pgvector")
    parser.add_argument("--dry-run", action="store_true", help="count and cost only, no API calls")
    parser.add_argument("--limit", type=int, help="embed at most N chunks (for a smoke test)")
    args = parser.parse_args(argv)

    load_dotenv()
    settings = Settings.from_env()
    model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
    dims = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    print("chunking silver…")
    pending = load_silver_chunks()
    print(f"  {len(pending)} chunks, {sum(c.tokens for c in pending):,} tokens total")

    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        todo = filter_already_embedded(conn, pending, model)
        skipped = len(pending) - len(todo)
        if skipped:
            print(f"  {skipped} already embedded for {model}, skipping")
        if args.limit:
            todo = todo[: args.limit]

        if not todo:
            print("\nnothing to embed — everything is current")
            return 0

        tokens = sum(c.tokens for c in todo)
        print(f"  {len(todo)} chunks to embed, {tokens:,} tokens")

        if args.dry_run:
            print("\ndry run — no API calls made")
            return 0

        if not api_key:
            print("OPENAI_API_KEY is not set. See docs/api-keys.md.", file=sys.stderr)
            return 1

        # The same rate-limited, Retry-After-aware client the harvester uses.
        http = HttpClient(TokenBucket(rate_per_minute=200), timeout=120)
        written = billed = 0
        for n, batch in enumerate(batches(todo), start=1):
            vectors, used = embed_batch(http, api_key, model, dims, [c.text for c in batch])
            written += insert(conn, batch, vectors, model)
            billed += used
            print(f"  batch {n}: {len(batch)} chunks, {used:,} tokens")

        total = conn.execute("SELECT count(*) FROM document_embeddings").fetchone()[0]
        print(f"\nwrote {written} rows ({billed:,} tokens billed). {total} total in pgvector.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
