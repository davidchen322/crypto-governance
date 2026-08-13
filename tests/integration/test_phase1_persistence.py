"""Phase 1 headline criterion: state survives `docker compose down`.

Run only *after* the stack has been torn down (without `-v`) and brought back up.
`scripts/verify.sh` sequences this; running it directly proves nothing.

Why `down` and not `restart`: `restart` never removes the containers, so it cannot
distinguish state held in a named volume from state held in a container's writable
layer. `down` removes the containers and keeps the volumes, which is exactly the
distinction that matters — and exactly what the original blueprint's SQLite-backed
catalog would have failed.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    SMOKE_NAMESPACE,
    SMOKE_ROW_NOTE,
    SMOKE_TABLE,
    run_spark_sql,
)

pytestmark = [pytest.mark.integration, pytest.mark.persistence]

CATALOG = "gov"


def test_catalog_metadata_survived_restart():
    """The table is still registered — proves the catalog is not ephemeral SQLite."""
    out = run_spark_sql(f"SHOW TABLES IN {CATALOG}.{SMOKE_NAMESPACE};")
    assert SMOKE_TABLE in out, (
        f"table {SMOKE_TABLE!r} vanished after restart — catalog metadata is not persistent:\n{out}"
    )


def test_row_data_survived_restart():
    out = run_spark_sql(f"SELECT note FROM {CATALOG}.{SMOKE_NAMESPACE}.{SMOKE_TABLE} WHERE id = 1;")
    assert SMOKE_ROW_NOTE in out, f"row did not survive restart:\n{out}"


def test_pgvector_extension_survived_restart(pg_dsn):
    import psycopg

    with psycopg.connect(pg_dsn, connect_timeout=10) as conn:
        assert conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone()


def test_bucket_survived_restart(s3_client, bucket):
    names = [b["Name"] for b in s3_client.list_buckets()["Buckets"]]
    assert bucket in names
