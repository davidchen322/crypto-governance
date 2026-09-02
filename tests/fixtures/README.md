# CI fixture — harvested once, embedded once, replayed forever

Solves a structural gap discovered while investigating a failing "Stack acceptance" CI job:
nothing in the tracked test suite or CI workflow ever called `data_pipeline.harvest` against
a real source, so Phase 3+ integration tests — which need real, populated silver and real
embeddings to mean anything — could never actually pass in CI. The obvious fix (harvest live
in CI) was rejected deliberately: it would mean every CI run depends on Snapshot's and
Discourse's uptime, which is exactly the flakiness this project's own `live` marker exists to
keep out of the default build.

Instead: harvest a small, real corpus **once**, embed it **once**, and commit both as fixtures.
CI restores them into a fresh stack on every run — real data, real Spark build, real pgvector
queries, **zero live network calls, zero API cost, zero secrets**, every single time.

## What's here

```
bronze/                     real Snapshot + Discourse JSON, exported verbatim (~1.4 MB)
embeddings.sql.gz           a real OpenAI embedding pass, both chunk schemes (~7.6 MB)
```

`scripts/restore_fixture.py` loads both into a running stack. `build_silver.py` then runs for
real, deterministically, against the restored bronze — that's what actually proves Spark
still works on every CI run, rather than freezing silver too and never exercising it.

## Scope

Two protocols — **Aave** and **Uniswap** — 12 proposals and 8 forum topics (with full post
bodies) each. Small enough to keep the fixture lean; real enough to span genuine governance
history (measured: **101 distinct governance dates**, 2020-09-21 to 2026-09-01) and to satisfy
every test that isn't marked `live`. Aave specifically is required by name in a couple of
existing tests (`ai_agent/graph/sql_templates.py`'s `list_proposals` template tests); Uniswap
was chosen as the second protocol for no reason more specific than "a second real DAO."

Everything marked `@pytest.mark.live` (real router/embedding-query calls, or assertions tied
to a specific protocol like `arbitrum` that isn't in this fixture) is unaffected by any of
this — those tests were never run in default CI and still aren't. This fixture only unblocks
the tests that were already meant to run by default.

## How it was generated

An isolated, throwaway Compose project (`COMPOSE_PROJECT_NAME=crypto-gov-fixture`, distinct
ports), so this never touched a real dev stack:

```bash
export COMPOSE_PROJECT_NAME=crypto-gov-fixture
export POSTGRES_PORT=55434 MINIO_PORT=9020 MINIO_CONSOLE_PORT=9021 \
       ICEBERG_REST_PORT=8183 TRINO_PORT=8092
docker compose up -d --wait postgres minio minio-init iceberg-rest spark trino

# the one real, deliberate harvest this fixture will ever need
MINIO_ENDPOINT=http://localhost:9020 ICEBERG_REST_URI=http://localhost:8183 \
  .venv/bin/python -m data_pipeline.harvest \
    --protocol aave --protocol uniswap --proposals 12 --topics 8 --with-posts

docker exec crypto-gov-fixture-spark-1 spark-submit --master "local[*]" \
  /opt/app/data_pipeline/transformation/build_silver.py

# embedded under v2 (the active scheme), then again under v1 (toggling
# ai_agent/chains/chunking.py's CHUNK_SCHEME constant temporarily and reverting it after) —
# both are needed for test_both_chunk_schemes_are_stored to mean anything real
POSTGRES_PORT=55434 TRINO_URL=http://localhost:8092 .venv/bin/python -m ai_agent.chains.embeddings
# ... toggle CHUNK_SCHEME to "v1", repeat, toggle back to "v2" ...

# export
.venv/bin/python -c '<download every object under snapshot/ and discourse/ to tests/fixtures/bronze/>'
docker exec crypto-gov-fixture-postgres-1 pg_dump -U engineer -d gov_vectors \
  --data-only --table=document_embeddings > tests/fixtures/embeddings.sql
gzip -9 tests/fixtures/embeddings.sql

docker compose down -v --remove-orphans   # the throwaway project, not the dev stack
```

Real, one-time cost: ~$0.04 (161,139 tokens × 2 schemes at `text-embedding-3-large`'s
published $0.13/1M-token rate). Nothing here ever calls OpenAI again.

## Regenerating

Only needed if chunking, embedding, or the SCD2 build logic changes in a way that would make
the frozen fixture stop matching what the current loader would produce — the same drift
`test_the_corpus_matches_what_the_loader_would_produce` exists to catch. Repeat the steps
above; a fresh harvest window will naturally pull the same or newer proposals from Snapshot
(nothing here is time-sensitive beyond "these are real proposals that existed when this was
generated").

Two of `tests/integration/test_phase4_retrieval.py`'s thresholds are sized to this fixture's
scale rather than to a full production corpus (`test_corpus_is_embedded`,
`test_both_chunk_schemes_are_stored`) — see the docstrings on those tests. Regenerating a
fixture at a meaningfully different scale should re-check those bounds still hold with margin.
