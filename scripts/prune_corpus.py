"""Remove already-embedded chunks that current curation rules would exclude.

The loader filters going forward; this reconciles what is already in `document_embeddings`.
Destructive, so it reports by default and only deletes when given `--apply`.

Two rules, applied differently because only one can be recomputed for a retired scheme:

  boilerplate (title-based)  — applied to EVERY chunk scheme. The rule depends only on the
                               thread's title, which does not change with chunking logic.
  runt chunks (token-based)  — applied to the ACTIVE scheme only, by re-deriving what the
                               loader would now produce and deleting whatever is absent.
                               A retired scheme's chunk boundaries cannot be recomputed by
                               today's chunker, and guessing at them would delete real rows.

Retired-scheme rows are deliberately kept otherwise: they exist so a chunking change can be
reverted without paying to re-embed, and pruning them would quietly destroy that option.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from ai_agent.chains.chunking import CHUNK_SCHEME
from ai_agent.chains.embeddings import load_silver_chunks
from config.corpus_filters import is_boilerplate_topic
from config.settings import Settings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: report only)")
    args = ap.parse_args()

    with psycopg.connect(Settings.from_env().postgres_dsn) as conn:
        titles = conn.execute(
            "SELECT DISTINCT title FROM document_embeddings "
            "WHERE source = 'forum' AND title IS NOT NULL"
        ).fetchall()
        boilerplate = sorted(t for (t,) in titles if is_boilerplate_topic(t))

        print(f"boilerplate threads matched: {len(boilerplate)}")
        for t in boilerplate:
            n = conn.execute(
                "SELECT count(*) FROM document_embeddings WHERE source='forum' AND title = %s",
                (t,),
            ).fetchone()[0]
            print(f"  {n:>4} chunk(s)  {t!r}")

        # Runt chunks: whatever the loader no longer produces for the active scheme.
        #
        # Keyed on (source, document_id, hash, index), NOT (hash, index). `content_hash` is
        # not unique across documents — every empty-bodied post hashes to sha256("") — so a
        # hash-only key let a stale ENS row survive because an unrelated Uniswap post shared
        # its hash and was still being produced.
        keep = {
            (c.source, c.document_id, c.source_content_hash, c.chunk_index)
            for c in load_silver_chunks()
        }
        current = conn.execute(
            "SELECT source, document_id, source_content_hash, chunk_index "
            "FROM document_embeddings WHERE chunk_scheme = %s",
            (CHUNK_SCHEME,),
        ).fetchall()
        orphans = [row for row in current if tuple(row) not in keep]
        print(f"\nchunks in {CHUNK_SCHEME} the loader no longer produces: {len(orphans)}")

        if not args.apply:
            print("\nreport only — re-run with --apply to delete")
            return 0

        deleted = 0
        with conn.cursor() as cur:
            for t in boilerplate:
                cur.execute(
                    "DELETE FROM document_embeddings WHERE source='forum' AND title = %s", (t,)
                )
                deleted += cur.rowcount
            if orphans:
                cur.executemany(
                    "DELETE FROM document_embeddings WHERE chunk_scheme = %s AND source = %s "
                    "AND document_id = %s AND source_content_hash = %s AND chunk_index = %s",
                    [(CHUNK_SCHEME, s, d, h, i) for s, d, h, i in orphans],
                )
                deleted += len(orphans)
        conn.commit()

        remaining = dict(
            conn.execute(
                "SELECT chunk_scheme, count(*) FROM document_embeddings GROUP BY 1"
            ).fetchall()
        )
        print(f"\ndeleted {deleted} row(s); remaining by scheme: {remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
