"""The scheduled pipeline: harvest a small window across all five protocols, fold it into
silver, embed what changed.

Most days this should do almost nothing — bronze is content-addressed (Phase 2), silver's
MERGE is change-guarded (Phase 3), and the embeddings loader skips what's already there
(Phase 4). A quiet run is the healthy, expected outcome, not something to investigate. What
IS worth investigating: a run that takes as long as a fresh harvest every single day with no
new governance activity to explain it — see docs/phase-7-plan.md's "How we will know it
works" for exactly what that looks like and why it's the quietest possible failure.

    python -m data_pipeline.harvest --protocol {p} --proposals 20 --topics 15 --with-posts
    docker exec crypto-gov-spark-1 spark-submit ... build_silver.py
    ai_agent.chains.embeddings.main([])

in that order, for every protocol in config.protocols.PROTOCOLS.
"""

from __future__ import annotations

import pendulum
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException

from dags.gov_common import DAILY_PROPOSALS, DAILY_TOPICS, build_silver_command, harvest_command

# A fixed date, never `datetime.now()` — Airflow computes schedule intervals from this and a
# moving start_date makes "when did this last actually run" unanswerable from the DAG file
# alone. Safely in the past relative to when this phase was written.
START_DATE = pendulum.datetime(2026, 8, 1, tz="UTC")


def _protocol_names() -> list[str]:
    # Imported inside the function, not at module scope: Airflow parses every DAG file in
    # the dags/ folder on a timer, and an import error at parse time takes the whole file
    # down silently in the UI rather than failing one task. Keeping the DAG file itself free
    # of anything but airflow + dags.gov_common imports is what test_dags_import_without_
    # error is actually checking.
    from config.protocols import PROTOCOLS

    return [p.name for p in PROTOCOLS]


@dag(
    dag_id="gov_pipeline_daily",
    schedule="@daily",
    start_date=START_DATE,
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": pendulum.duration(minutes=5)},
    tags=["governance", "daily"],
)
def gov_pipeline_daily():
    @task.bash
    def harvest(protocol: str) -> str:
        return harvest_command(protocol, DAILY_PROPOSALS, DAILY_TOPICS)

    @task.bash
    def build_silver() -> str:
        return build_silver_command()

    @task
    def embed() -> dict:
        from ai_agent.chains.embeddings import main as embed_main

        rc = embed_main([])
        if rc != 0:
            # main() itself only returns non-zero when OPENAI_API_KEY is unset — a
            # misconfigured environment, not a transient failure retries would fix. Failing
            # loudly here is what turns "quietly never embedded anything, forever" into a
            # task the Airflow UI shows red.
            raise AirflowFailException(f"embeddings.main() exited {rc}")
        return {"exit_code": rc}

    harvested = harvest.expand(protocol=_protocol_names())
    harvested >> build_silver() >> embed()


gov_pipeline_daily()
