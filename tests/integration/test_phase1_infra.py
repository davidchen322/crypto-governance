"""Phase 1 acceptance: every service does its job, not merely 'reports healthy'.

Each probe exercises the path the application will actually use. A container can be
`healthy` while the service inside it is unusable, so none of these assert on
`docker compose ps` output.
"""

from __future__ import annotations

import json

import pytest
import requests

from tests.integration.conftest import (
    SMOKE_NAMESPACE,
    SMOKE_ROW_NOTE,
    SMOKE_TABLE,
    run_spark_sql,
)

pytestmark = pytest.mark.integration

CATALOG = "gov"


# --------------------------------------------------------------------------
# Postgres
# --------------------------------------------------------------------------


def test_postgres_accepts_host_connections(pg_dsn):
    """Probed from the host, so a broken port mapping fails here."""
    import psycopg

    with psycopg.connect(pg_dsn, connect_timeout=10) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_pgvector_extension_is_usable(pg_dsn):
    """`pg_isready` returns success long before this works — that is the point."""
    import psycopg

    with psycopg.connect(pg_dsn, connect_timeout=10) as conn:
        installed = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
        assert installed, "the `vector` extension is not installed in the vector database"

        distance = conn.execute("SELECT '[1,2,3]'::vector <=> '[3,2,1]'::vector").fetchone()[0]
        assert 0.0 < float(distance) < 1.0, f"cosine operator returned {distance!r}"


def test_hnsw_index_can_be_built(pg_dsn):
    """Phase 4 depends on this; failing now is cheaper than failing then."""
    import psycopg

    with psycopg.connect(pg_dsn, connect_timeout=10, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS _probe_vectors")
        conn.execute("CREATE TABLE _probe_vectors (id int, embedding vector(3))")
        conn.execute("INSERT INTO _probe_vectors VALUES (1, '[1,2,3]'), (2, '[4,5,6]')")
        conn.execute(
            "CREATE INDEX _probe_hnsw ON _probe_vectors USING hnsw (embedding vector_cosine_ops)"
        )
        nearest = conn.execute(
            "SELECT id FROM _probe_vectors ORDER BY embedding <=> '[1,2,3]' LIMIT 1"
        ).fetchone()[0]
        assert nearest == 1
        conn.execute("DROP TABLE _probe_vectors")


def test_all_three_databases_exist(pg_dsn):
    import psycopg

    with psycopg.connect(pg_dsn, connect_timeout=10) as conn:
        names = {r[0] for r in conn.execute("SELECT datname FROM pg_database").fetchall()}
    assert {"gov_vectors", "iceberg_catalog", "airflow"} <= names, (
        f"expected three databases, found {sorted(names)}"
    )


# --------------------------------------------------------------------------
# MinIO
# --------------------------------------------------------------------------


def test_warehouse_bucket_exists(s3_client, bucket):
    """Catches the stack that only works because someone made the bucket by hand."""
    names = [b["Name"] for b in s3_client.list_buckets()["Buckets"]]
    assert bucket in names, f"bucket {bucket!r} missing; found {names}"


def test_minio_round_trip(s3_client, bucket):
    """`list_buckets` succeeding does not prove writes land."""
    key = "_probe/smoke.txt"
    s3_client.put_object(Bucket=bucket, Key=key, Body=b"phase1")
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    assert body == b"phase1"
    s3_client.delete_object(Bucket=bucket, Key=key)


# --------------------------------------------------------------------------
# Iceberg REST catalog
# --------------------------------------------------------------------------


def test_catalog_serves_the_rest_spec(catalog_uri):
    """An open TCP port is not a working catalog — this hits the real spec endpoint."""
    resp = requests.get(f"{catalog_uri}/v1/config", timeout=15)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:400]}"
    payload = resp.json()
    assert "defaults" in payload or "overrides" in payload, (
        f"unexpected /v1/config body: {json.dumps(payload)[:400]}"
    )


# --------------------------------------------------------------------------
# Spark -> catalog -> MinIO, end to end
# --------------------------------------------------------------------------


@pytest.mark.seed
def test_spark_writes_iceberg_table_through_catalog():
    """The integration that matters: all three services on one code path."""
    out = run_spark_sql(
        f"""
        CREATE NAMESPACE IF NOT EXISTS {CATALOG}.{SMOKE_NAMESPACE};
        DROP TABLE IF EXISTS {CATALOG}.{SMOKE_NAMESPACE}.{SMOKE_TABLE};
        CREATE TABLE {CATALOG}.{SMOKE_NAMESPACE}.{SMOKE_TABLE} (id INT, note STRING)
            USING iceberg;
        INSERT INTO {CATALOG}.{SMOKE_NAMESPACE}.{SMOKE_TABLE}
            VALUES (1, '{SMOKE_ROW_NOTE}');
        SELECT note FROM {CATALOG}.{SMOKE_NAMESPACE}.{SMOKE_TABLE} WHERE id = 1;
        """
    )
    assert SMOKE_ROW_NOTE in out, f"row not returned by Spark:\n{out}"


@pytest.mark.seed
def test_table_data_actually_landed_in_minio(s3_client, bucket):
    """Guards the misconfigured-S3FileIO case: catalog knows the table, data went elsewhere."""
    prefix = f"{SMOKE_NAMESPACE}/{SMOKE_TABLE}"
    listing = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    keys = [o["Key"] for o in listing.get("Contents", [])]
    assert keys, f"no objects under s3://{bucket}/{prefix} — data did not land in MinIO"
    assert any(k.endswith(".parquet") for k in keys), f"no parquet written; got {keys[:10]}"
    assert any("metadata" in k for k in keys), f"no iceberg metadata written; got {keys[:10]}"
