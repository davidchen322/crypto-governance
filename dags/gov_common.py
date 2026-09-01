"""Shared between `gov_pipeline_daily` and `gov_backfill`.

Both DAGs run the same three-stage pipeline — harvest, build silver, embed — differing only
in *how much* they harvest and whether they check a cost ceiling first. Command construction
lives here once so the two DAGs cannot silently drift into two different implementations of
"run the harvester."

Nothing here imports `airflow`. That is deliberate: this module is plain Python, importable
and unit-testable (`tests/test_phase7_dags.py`) without an Airflow installation, a database,
or a running scheduler. Only the DAG files themselves import `airflow`.
"""

from __future__ import annotations

import os

# The container `docker exec` targets. Compose's fixed `name: crypto-gov` project prefix
# (see docker-compose.yml) makes this deterministic as long as nobody starts the dev stack
# under a different project name — the override exists for that one case, not because the
# default is expected to change.
SPARK_CONTAINER = os.getenv("SPARK_CONTAINER_NAME", "crypto-gov-spark-1")

BUILD_SILVER_SCRIPT = "/opt/app/data_pipeline/transformation/build_silver.py"

# The daily DAG's harvest window. Matches every demo and every eval corpus built so far —
# changing these here changes what "a normal day" looks like for every phase's existing
# baselines, so don't.
DAILY_PROPOSALS = 20
DAILY_TOPICS = 15

# Backfill starting point. Cheap to raise: bronze is content-addressed, so a second, larger
# run only fetches what the first one didn't reach. See docs/phase-7-plan.md's "Sizing the
# historical backfill" for why these are starting points, not computed targets — Discourse's
# true per-protocol topic volume is genuinely undocumented anywhere in this repo.
BACKFILL_PROPOSALS = 1000
BACKFILL_TOPICS = 500

# Phase 4's own measured rate: $0.055 for 419,826 tokens = ~$0.131 per 1,000,000 tokens on
# text-embedding-3-large (matches OpenAI's published $0.13/1M rate). A *default*, overridable
# via the `embedding_cost_per_1m_tokens` Airflow Variable — check current published OpenAI
# rates before trusting it, exactly per the project's existing convention (see
# implementation-plan.md's Cost section).
#
# Found and fixed during Phase 7's own build: an earlier version of this constant was named
# and valued as "per 1,000 tokens" while carrying the per-1,000,000 figure — a dropped-zeros
# transcription of the Phase 4 arithmetic, not a units disagreement with OpenAI's own pricing.
# It inflated every cost estimate by exactly 1000x, so `check_cost_ceiling` was validating
# real backfill runs against invented $76+ price tags for what actually cost ~$0.08. The
# failure direction was the safe one — it over-blocks rather than under-warns — but it made
# the ceiling check meaningless in practice, since a correctly-priced backfill would almost
# always look catastrophically expensive.
DEFAULT_COST_PER_1M_TOKENS = 0.131
DEFAULT_COST_CEILING_USD = 5.00


def harvest_command(protocol: str, proposals: int, topics: int) -> str:
    """The exact CLI `make harvest` already runs by hand, parameterized for one protocol."""
    return (
        f"python -m data_pipeline.harvest --protocol {protocol} "
        f"--proposals {proposals} --topics {topics} --with-posts"
    )


def build_silver_command() -> str:
    """The exact command `make silver` runs, via `docker exec` rather than `docker compose
    exec` so this works without the `docker compose` CLI plugin installed in the Airflow
    image — the base `docker` client is enough to exec into an already-running container by
    name. No `-t`: there is no TTY in a scheduled task, matching `make silver`'s own `-T`."""
    return f'docker exec {SPARK_CONTAINER} spark-submit --master "local[*]" {BUILD_SILVER_SCRIPT}'


def estimate_embedding_cost(conn, model: str) -> tuple[int, int]:
    """(tokens still to embed, chunks still to embed) — computed the same way `make embed
    ARGS=--dry-run` does, by calling the same functions rather than parsing that command's
    printed output. Spends nothing; makes no API call."""
    from ai_agent.chains.embeddings import filter_already_embedded, load_silver_chunks

    pending = load_silver_chunks()
    todo = filter_already_embedded(conn, pending, model)
    return sum(c.tokens for c in todo), len(todo)
