"""Central settings, read from the environment.

Every key referenced here must also appear in `.env.example` — enforced by
tests/test_phase0_repo.py so a new variable cannot silently become tribal knowledge.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Read .env into the environment, without adding a dependency for four lines.

    Values already set in the environment win, so `OPENAI_API_KEY=... make embed` overrides
    the file. Every entry point that needs credentials calls this — an earlier version
    lived only in scripts/check_openai.py, so the credential check passed while the loader
    that actually needed the key reported it unset.
    """
    path = path or REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Settings:
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str
    postgres_vector_db: str
    postgres_catalog_db: str
    postgres_airflow_db: str

    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str

    iceberg_rest_uri: str
    iceberg_catalog_name: str
    iceberg_warehouse: str

    aws_region: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            postgres_host=os.getenv("POSTGRES_HOST", "localhost"),
            postgres_port=int(os.getenv("POSTGRES_PORT", "55432")),
            postgres_user=os.getenv("POSTGRES_USER", "engineer"),
            postgres_password=os.getenv("POSTGRES_PASSWORD", "localdev"),
            postgres_vector_db=os.getenv("POSTGRES_VECTOR_DB", "gov_vectors"),
            postgres_catalog_db=os.getenv("POSTGRES_CATALOG_DB", "iceberg_catalog"),
            postgres_airflow_db=os.getenv("POSTGRES_AIRFLOW_DB", "airflow"),
            minio_endpoint=os.getenv("MINIO_ENDPOINT", "http://localhost:9000"),
            minio_access_key=os.getenv("MINIO_ROOT_USER", "minioadmin"),
            minio_secret_key=os.getenv("MINIO_ROOT_PASSWORD", "minioadmin"),
            minio_bucket=os.getenv("MINIO_BUCKET", "warehouse"),
            iceberg_rest_uri=os.getenv("ICEBERG_REST_URI", "http://localhost:8181"),
            iceberg_catalog_name=os.getenv("ICEBERG_CATALOG_NAME", "gov"),
            iceberg_warehouse=os.getenv("ICEBERG_WAREHOUSE", "s3://warehouse/"),
            aws_region=os.getenv("AWS_REGION", "us-east-1"),
        )

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_vector_db}"
        )
