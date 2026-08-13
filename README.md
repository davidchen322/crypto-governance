# Crypto Governance Intelligence Platform

Monitors public governance activity across DAOs — Snapshot proposals, protocol forum
debate, and (later) on-chain contract source — and answers questions about it with
citations.

The full build plan lives in [`docs/implementation-plan.md`](docs/implementation-plan.md).
**Phases 0 and 1 are complete**; results are in
[`docs/phase-0-1-results.md`](docs/phase-0-1-results.md).

## Quick start

```bash
make install     # venv + dependencies
make up          # start the stack, block until healthy
make verify      # full Phase 0 + 1 acceptance harness
```

`make help` lists every target.

## What runs locally

| Service | Host port | Role |
| --- | --- | --- |
| Postgres + pgvector | 55432 | Vectors, Iceberg catalog backend, Airflow metadata |
| MinIO | 9000 / 9001 | S3-compatible object store (console on 9001) |
| Iceberg REST catalog | 8181 | Table catalog, persisted to Postgres |
| Spark | — | Local mode, Iceberg jars baked in |

Everything is free. Only the embedding and LLM calls from Phase 4 onward cost money.

Postgres is on **55432**, not 5432, because a locally installed Postgres on the default
port makes a working stack look broken.

## Verification

`make verify` is the acceptance gate, and its exit code is the verdict. It does not read
`docker compose ps` — a container reports healthy long before the service inside it is
usable. Instead it:

1. Checks repo hygiene: no `.env` tracked, every env var documented.
2. Destroys volumes and rebuilds from zero, proving the stack needs no manual setup.
3. Probes each service on the path the application actually uses — pgvector's cosine
   operator, a MinIO write/read round trip, the catalog's `/v1/config` endpoint, and a
   Spark write that must land real Parquet in MinIO.
4. Runs `docker compose down` (volumes retained) and brings the stack back.
5. Re-asserts that the catalog metadata, table data, extension and bucket all survived.

Step 5 is the one that matters. `docker compose restart` would pass trivially; `down`
removes the containers, so only state genuinely held in named volumes survives.

## Layout

```
config/          settings read from the environment
data_pipeline/   extraction, orchestration, transformation
storage/         DDL for the lakehouse and vector store
ai_agent/        RAG and LangGraph logic
backend_api/     FastAPI delivery layer; saas_modules/ stays isolated
docker/          service images and init hooks
scripts/         verify.sh — the acceptance harness
tests/           unit tests; tests/integration/ needs the stack running
```
