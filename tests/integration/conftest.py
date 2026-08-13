"""Shared helpers for Phase 1 infrastructure probes.

Design note: Postgres, MinIO and the Iceberg catalog are probed **from the host** so a
broken port mapping fails the test. Spark is probed **inside its container**, because it
must reach MinIO and the catalog over the compose network — the two views catch
different classes of misconfiguration.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

SMOKE_NAMESPACE = "demo"
SMOKE_TABLE = "smoke"
SMOKE_ROW_NOTE = "phase1-persistence-probe"


def env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    return (
        f"host={env('POSTGRES_HOST', 'localhost')} "
        f"port={env('POSTGRES_PORT', '55432')} "
        f"user={env('POSTGRES_USER', 'engineer')} "
        f"password={env('POSTGRES_PASSWORD', 'localdev')} "
        f"dbname={env('POSTGRES_VECTOR_DB', 'gov_vectors')}"
    )


@pytest.fixture(scope="session")
def s3_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=env("MINIO_ENDPOINT", "http://localhost:9000"),
        aws_access_key_id=env("MINIO_ROOT_USER", "minioadmin"),
        aws_secret_access_key=env("MINIO_ROOT_PASSWORD", "minioadmin"),
        region_name=env("AWS_REGION", "us-east-1"),
    )


@pytest.fixture(scope="session")
def bucket() -> str:
    return env("MINIO_BUCKET", "warehouse")


@pytest.fixture(scope="session")
def catalog_uri() -> str:
    return env("ICEBERG_REST_URI", "http://localhost:8181")


def run_spark_sql(sql: str, timeout: int = 420) -> str:
    """Run SQL through spark-sql inside the spark container.

    Batches statements into a single invocation — spark-sql startup dominates runtime,
    so one call with several statements is far cheaper than several calls.
    """
    proc = subprocess.run(
        [
            "docker", "compose", "exec", "-T", "spark",
            "spark-sql", "--silent", "-e", sql,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"spark-sql failed (exit {proc.returncode})\n"
            f"--- SQL ---\n{sql}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr[-4000:]}"
        )
    return proc.stdout
