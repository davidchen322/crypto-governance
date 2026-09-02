"""Restore the committed CI fixture into a fresh stack — no live network calls, ever.

    python scripts/restore_fixture.py            # bronze + embeddings
    python scripts/restore_fixture.py --bronze-only
    python scripts/restore_fixture.py --embeddings-only

Two pieces, generated once (see tests/fixtures/README.md for exactly how) and replayed here
on every run:

  tests/fixtures/bronze/**          real Snapshot + Discourse JSON, harvested once, exported
                                     verbatim. Restoring these is a plain upload — build_silver
                                     then runs for REAL, deterministically, against them,
                                     which is what actually exercises Spark in CI.
  tests/fixtures/embeddings.sql.gz  a real OpenAI embedding pass over that same corpus (both
                                     chunk schemes), run once and captured. Restoring this
                                     skips the one step that would otherwise need a live API
                                     key and a live network call on every CI run.

Bronze restoration works against MinIO directly, so it needs no code from this project beyond
`config.settings`. Embeddings restoration drives `psycopg`'s own COPY support directly —
matching how every other script in this project talks to Postgres — rather than assuming a
`psql` client binary is on PATH, which is not guaranteed on every machine this might run on.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from pathlib import Path

import boto3
import psycopg

from config.settings import Settings, load_dotenv

FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
BRONZE_DIR = FIXTURE_ROOT / "bronze"
EMBEDDINGS_DUMP = FIXTURE_ROOT / "embeddings.sql.gz"


def restore_bronze(settings: Settings) -> int:
    if not BRONZE_DIR.is_dir():
        print(f"no fixture bronze at {BRONZE_DIR}", file=sys.stderr)
        return 1

    s3 = boto3.client(
        "s3",
        endpoint_url=settings.minio_endpoint,
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        region_name=settings.aws_region,
    )
    written = 0
    for path in BRONZE_DIR.rglob("*.json"):
        key = str(path.relative_to(BRONZE_DIR))
        s3.upload_file(str(path), settings.minio_bucket, key)
        written += 1
    print(f"restored {written} bronze object(s) to s3://{settings.minio_bucket}/")
    return 0


COPY_HEADER = re.compile(r"^COPY\s+(\S+)\s+\(([^)]+)\)\s+FROM\s+stdin;\s*$")


def restore_embeddings(settings: Settings) -> int:
    """Parse the one `COPY ... FROM stdin ... \\.` block a plain-format `pg_dump
    --data-only` produces and replay it through psycopg's own COPY protocol support.

    Deliberately not `psql -f`: this project never assumes a `psql` client binary is on
    PATH (every other script here talks to Postgres through psycopg directly), and a
    single-table data dump is a small, easy format to parse without depending on one.
    """
    if not EMBEDDINGS_DUMP.exists():
        print(f"no fixture embeddings dump at {EMBEDDINGS_DUMP}", file=sys.stderr)
        return 1

    with gzip.open(EMBEDDINGS_DUMP, "rt") as f:
        lines = f.readlines()

    table, columns, data_start = None, None, None
    for i, line in enumerate(lines):
        m = COPY_HEADER.match(line)
        if m:
            table, columns, data_start = m.group(1), m.group(2), i + 1
            break
    if data_start is None:
        print("no COPY block found in the fixture dump", file=sys.stderr)
        return 1

    data_end = next(i for i in range(data_start, len(lines)) if lines[i].rstrip("\n") == "\\.")
    payload = "".join(lines[data_start:data_end])

    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        with conn.cursor().copy(f"COPY {table} ({columns}) FROM STDIN") as copy:
            copy.write(payload)
        count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    print(f"restored document_embeddings from the fixture dump — {count} rows now present")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bronze-only", action="store_true")
    parser.add_argument("--embeddings-only", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv()
    settings = Settings.from_env()

    rc = 0
    if not args.embeddings_only:
        rc |= restore_bronze(settings)
    if not args.bronze_only:
        rc |= restore_embeddings(settings)
    return rc


if __name__ == "__main__":
    sys.exit(main())
