"""`/health` — a real dependency check, not a constant.

The plan's correction to the source blueprint reads "a real `/health` that checks Postgres
and the catalog rather than returning a constant." This checks Postgres (the vector store)
and Trino rather than the raw Iceberg REST catalog the blueprint named: since Phase 5, every
live query the SQL and hybrid nodes run — and both `/proposals` routes — goes through Trino,
not through the catalog directly. A catalog-only check would report healthy while a query
through Trino still failed; Trino is what actually predicts whether `/api/v1/chat` and
`/proposals` will work.

Returns 503 rather than 200 when a dependency is down, so a load balancer or uptime check
can act on it — the whole reason this exists instead of `{"status": "ok"}`.
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Response

from ai_agent.chains.trino_client import query as trino_query
from backend_api.schemas import HealthComponent, HealthResponse
from config.settings import Settings

router = APIRouter(tags=["health"])


def _check_postgres() -> HealthComponent:
    try:
        with psycopg.connect(Settings.from_env().postgres_dsn, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
        return HealthComponent(status="ok")
    except Exception as exc:
        return HealthComponent(status="error", detail=str(exc))


def _check_trino() -> HealthComponent:
    try:
        trino_query("SELECT 1")
        return HealthComponent(status="ok")
    except Exception as exc:
        return HealthComponent(status="error", detail=str(exc))


@router.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    postgres = _check_postgres()
    trino = _check_trino()
    ok = postgres.status == "ok" and trino.status == "ok"
    response.status_code = 200 if ok else 503
    return HealthResponse(status="ok" if ok else "degraded", postgres=postgres, trino=trino)
