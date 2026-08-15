# Crypto Governance Intelligence Platform

Monitors public governance activity across DAOs — Snapshot proposals, protocol forum
debate, and (later) on-chain contract source — and answers questions about it with
citations.

The full build plan lives in [`docs/implementation-plan.md`](docs/implementation-plan.md).
**Phases 0 through 3 are complete**; results are in
[`docs/phase-0-1-results.md`](docs/phase-0-1-results.md),
[`docs/phase-2-results.md`](docs/phase-2-results.md) and
[`docs/phase-3-results.md`](docs/phase-3-results.md). Styled build reports are
published as private artifacts; links are in the commit messages.

Querying the data locally: [`docs/querying.md`](docs/querying.md).
External sources and which need an API key: [`docs/api-keys.md`](docs/api-keys.md).
Short version — nothing so far needs a credential; the first required one is OpenAI, at
Phase 4, for embeddings.

## Quick start

```bash
make install     # venv + dependencies
make up          # start the stack, block until healthy
make verify      # full acceptance harness
make harvest ARGS="--all --proposals 20 --topics 15 --with-posts"
make silver       # fold bronze into the SCD2 Iceberg tables
make sql          # interactive Trino shell (fast)
```

`make help` lists every target.

## Harvesting

```bash
make harvest ARGS="--protocol aave --proposals 25"
make harvest ARGS="--all --topics 30 --with-posts"
```

Raw API responses land in the `warehouse` bucket under content-addressed keys:

```
snapshot/space=aavedao.eth/<proposal_id>/<content_hash>.json
discourse/space=governance.aave.com/<topic_id>/<content_hash>.json
_manifests/<run_id>.json
```

No date appears in a data key. Re-harvesting unchanged content resolves to a key that
already exists, so the write is skipped; an edited document hashes differently and lands
*beside* its previous version rather than overwriting it. Bronze therefore accumulates the
version history that Phase 3's SCD2 tables are built from, and makes an embedding-model
change replayable without re-fetching anything.

Coverage is five protocols — Aave, Uniswap, Arbitrum, Optimism, ENS — configured in
[`config/protocols.py`](config/protocols.py) with Snapshot space ids verified against the
live API.

## What runs locally

| Service | Host port | Role |
| --- | --- | --- |
| Postgres + pgvector | 55432 | Vectors, Iceberg catalog backend, Airflow metadata |
| MinIO | 9000 / 9001 | S3-compatible object store (console on 9001) |
| Iceberg REST catalog | 8181 | Table catalog, persisted to Postgres |
| Spark | — | Local mode, Iceberg jars baked in |
| Trino | 8090 | Interactive SQL + JDBC over the same tables |

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

**The harness runs under its own Compose project** (`crypto-gov-verify`) with separate
volumes and host ports. It destroys volumes by design, so it must never be able to take
harvested data with it — re-fetching a large backfill from rate-limited public APIs because
someone ran the test suite is not an acceptable failure mode. Both stacks can run at once.
`make nuke`, by contrast, destroys the dev stack's volumes: that is what the name is for.

Tests that hit Snapshot and Discourse are marked `live` and excluded from `make verify` —
a red build caused by someone else's maintenance window teaches nothing. Run them
deliberately with `make verify-live` when changing a client or when a harvest starts
returning something unexpected; their job is to catch upstream API drift.

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
