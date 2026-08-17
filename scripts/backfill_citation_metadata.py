"""Backfill `title` and `document_date` onto embeddings that predate those columns.

Metadata only — no embedding is read, written, or paid for. The vectors are unchanged;
this fills in the two fields needed to show a result to a human.

Keyed on `source_content_hash`, which is exactly the right key: it is silver's identity for
the text a chunk was built from, so every chunk of a document resolves to that document's
title and date. It also means a row whose text changed gets the metadata of the version it
was actually embedded from, rather than today's.

Idempotent — re-running updates the same rows to the same values.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from ai_agent.chains.trino_client import query as trino_query
from config.settings import Settings

# `is_current` is deliberately absent. Embeddings can reference superseded versions, and
# restricting to current rows would leave those permanently null.
SILVER = """
    SELECT content_hash, title, cast(proposal_created AS varchar) AS document_date
    FROM iceberg.silver.proposal_versions
    UNION ALL
    SELECT content_hash, topic_title AS title, cast(post_created_at AS varchar) AS document_date
    FROM iceberg.silver.forum_posts
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report what would change")
    args = ap.parse_args()

    rows = trino_query(SILVER)
    # One content_hash can appear more than once (identical boilerplate across forums, the
    # same 11-row collapse seen in silver). Any of them carries the same text, so first wins.
    by_hash: dict[str, tuple[str | None, str | None]] = {}
    for r in rows:
        by_hash.setdefault(r["content_hash"], (r["title"], r["document_date"]))
    print(f"silver: {len(rows)} rows, {len(by_hash)} distinct content hashes")

    with psycopg.connect(Settings.from_env().postgres_dsn) as conn:
        missing = conn.execute(
            "SELECT count(*) FROM document_embeddings WHERE title IS NULL OR document_date IS NULL"
        ).fetchone()[0]
        print(f"pgvector: {missing} chunk(s) missing citation metadata")
        if args.dry_run:
            return 0

        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE document_embeddings SET title = %s, document_date = %s "
                "WHERE source_content_hash = %s",
                [(t, d or None, h) for h, (t, d) in by_hash.items()],
            )
        conn.commit()

        still = conn.execute(
            "SELECT count(*) FROM document_embeddings WHERE title IS NULL OR document_date IS NULL"
        ).fetchone()[0]
        print(f"done: {missing - still} filled, {still} still null")
        if still:
            print("  (a chunk whose content_hash is absent from silver — investigate)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
