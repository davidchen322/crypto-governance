"""The historical backfill: full protocol history, not the daily DAG's recent slice.

Manually triggered only (`schedule=None`). Everything sizeable is a run-time parameter, read
from the triggering run's config rather than hardcoded, so a first pass at 1000
proposals / 500 topics and a later, larger re-trigger for one forum that's still finding new
history are the same DAG, not two.

Trigger with config, from the Airflow UI ("Trigger DAG w/ config") or the CLI:

    airflow dags trigger gov_backfill --conf '{"protocols": ["aave"], "topics": 1500}'

Any key left out of `--conf` falls back to BACKFILL_PROPOSALS / BACKFILL_TOPICS / every
configured protocol. See docs/phase-7-plan.md's "Sizing the historical backfill" for how to
tell when a protocol's forum history has actually been reached rather than merely requested.

Before spending anything on embeddings, `check_cost_ceiling` estimates the bill from the real
corpus (via the same functions `make embed ARGS=--dry-run` uses) and compares it against the
`backfill_cost_ceiling_usd` Airflow Variable. Going over that ceiling fails the task with the
estimate in the log — never a silent, arbitrarily truncated embed.
"""

from __future__ import annotations

import pendulum
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException
from airflow.models.param import Param
from airflow.operators.python import get_current_context

from dags.gov_common import (
    BACKFILL_PROPOSALS,
    BACKFILL_TOPICS,
    DEFAULT_COST_CEILING_USD,
    DEFAULT_COST_PER_1K_TOKENS,
    build_silver_command,
    estimate_embedding_cost,
    harvest_command,
)

START_DATE = pendulum.datetime(2026, 8, 1, tz="UTC")


@dag(
    dag_id="gov_backfill",
    schedule=None,
    start_date=START_DATE,
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": pendulum.duration(minutes=5)},
    tags=["governance", "backfill"],
    params={
        # Param(...) validates what a human can type into "Trigger DAG w/ config" — a typo'd
        # protocol name or a negative count fails at trigger time, in the UI, not three tasks
        # deep into a run that's already spent rate-limit budget.
        "protocols": Param(
            default=None,
            type=["null", "array"],
            description="Protocol names to backfill. Omit or null for every configured protocol.",
        ),
        "proposals": Param(BACKFILL_PROPOSALS, type="integer", minimum=1),
        "topics": Param(BACKFILL_TOPICS, type="integer", minimum=0),
    },
)
def gov_backfill():
    @task
    def resolve_protocols() -> list[str]:
        from config.protocols import resolve

        ctx = get_current_context()
        # `resolve(None)` returns every configured protocol; `resolve([...])` validates the
        # names and raises on a typo rather than silently harvesting nothing for it. Read via
        # get_current_context() rather than a Jinja-templated argument, which would need
        # render_template_as_native_obj=True to preserve None/list rather than stringifying it.
        return [p.name for p in resolve(ctx["params"]["protocols"])]

    @task.bash
    def harvest(protocol: str) -> str:
        ctx = get_current_context()
        params = ctx["params"]
        return harvest_command(protocol, params["proposals"], params["topics"])

    @task.bash
    def build_silver() -> str:
        return build_silver_command()

    @task(retries=0)  # an over-budget estimate won't change on retry; ask a human instead
    def check_cost_ceiling() -> dict:
        import os

        import psycopg
        from airflow.models import Variable

        from config.settings import Settings

        model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
        rate = float(
            Variable.get("embedding_cost_per_1k_tokens", default_var=DEFAULT_COST_PER_1K_TOKENS)
        )
        ceiling = float(
            Variable.get("backfill_cost_ceiling_usd", default_var=DEFAULT_COST_CEILING_USD)
        )

        with psycopg.connect(Settings.from_env().postgres_dsn) as conn:
            tokens, chunks = estimate_embedding_cost(conn, model)

        estimated_cost = tokens / 1000 * rate
        report = {
            "chunks_to_embed": chunks,
            "tokens_to_embed": tokens,
            "rate_per_1k_tokens": rate,
            "estimated_cost_usd": round(estimated_cost, 4),
            "ceiling_usd": ceiling,
        }
        if estimated_cost > ceiling:
            raise AirflowFailException(
                f"Estimated embedding cost ${estimated_cost:.2f} for {chunks} chunks "
                f"({tokens:,} tokens) exceeds the ${ceiling:.2f} ceiling. Raise the "
                "'backfill_cost_ceiling_usd' Airflow Variable to proceed, or re-trigger "
                f"with smaller params.topics / params.proposals. Detail: {report}"
            )
        return report

    @task
    def embed() -> dict:
        from ai_agent.chains.embeddings import main as embed_main

        rc = embed_main([])
        if rc != 0:
            raise AirflowFailException(f"embeddings.main() exited {rc}")
        return {"exit_code": rc}

    protocols = resolve_protocols()
    harvested = harvest.expand(protocol=protocols)
    harvested >> build_silver() >> check_cost_ceiling() >> embed()


gov_backfill()
