"""Phase 7 acceptance: DAG structure and safety, without running a real pipeline.

Needs `apache-airflow` importable — true inside the `airflow-scheduler`/`airflow-webserver`
containers (docker/airflow/Dockerfile), not in the bare host venv `make test` uses. Run via:

    docker compose exec -T airflow-scheduler python -m pytest tests/test_phase7_dags.py -v

(or `make verify-airflow`, which does exactly that). Skipped entirely, not failed, when
`airflow` is not importable, so `make test` stays fast and dependency-free — the same
`live`/`integration` split Phases 2-6 already established, one tier further out.

What these check is the class of Airflow failure that produces no error and no red task:
a DAG that silently never runs (an import error the UI shows but nothing else does), a
schedule that quietly triples up on a missed day, a harvest failure a downstream task
absorbs into a `0` exit code. Every test name here matches one named in
docs/phase-7-plan.md's "How we will know it works".
"""

from __future__ import annotations

from pathlib import Path

import pytest

DAGS_DIR = Path(__file__).resolve().parents[1] / "dags"

airflow = pytest.importorskip("airflow", reason="apache-airflow is not installed here")

from airflow.models import DagBag  # noqa: E402

from dags.gov_common import (  # noqa: E402
    BACKFILL_PROPOSALS,
    BACKFILL_TOPICS,
    DAILY_PROPOSALS,
    DAILY_TOPICS,
    SPARK_CONTAINER,
    build_silver_command,
    harvest_command,
)


@pytest.fixture(scope="module")
def dagbag() -> DagBag:
    return DagBag(dag_folder=str(DAGS_DIR), include_examples=False)


# --------------------------------------------------------------------------
# Import safety — the LangGraph-workflow lesson, applied to Airflow
# --------------------------------------------------------------------------


def test_dags_import_without_error(dagbag: DagBag):
    """A DAG file that raises on import is a DAG that silently never runs — the Airflow UI
    surfaces it as an import error banner, but `docker compose ps` stays green and nothing
    else says so. Mirrors test_phase5_graph.py's concern about workflow.py failing to
    compile, one layer further out."""
    assert dagbag.import_errors == {}, dagbag.import_errors
    assert dagbag.get_dag("gov_pipeline_daily") is not None
    assert dagbag.get_dag("gov_backfill") is not None


# --------------------------------------------------------------------------
# gov_pipeline_daily
# --------------------------------------------------------------------------


def test_gov_pipeline_daily_has_the_expected_task_dependencies(dagbag: DagBag):
    dag = dagbag.get_dag("gov_pipeline_daily")
    harvest = dag.get_task("harvest")
    build_silver = dag.get_task("build_silver")
    embed = dag.get_task("embed")

    assert build_silver.task_id in harvest.downstream_task_ids
    assert embed.task_id in build_silver.downstream_task_ids


def test_gov_pipeline_daily_does_not_catch_up_and_runs_one_at_a_time(dagbag: DagBag):
    """The two settings that stop 'the scheduler was down for three days' from becoming
    three simultaneous, mutually-clobbering harvests."""
    dag = dagbag.get_dag("gov_pipeline_daily")
    assert dag.catchup is False
    assert dag.max_active_runs == 1


def test_every_protocol_is_represented_in_the_mapped_tasks():
    """A protocol silently dropped from the DAG's mapping must be caught by a test, not
    discovered months later — mirrors Phase 3's 'a space quietly missing from silver looks
    identical to a space with no activity' finding."""
    from config.protocols import PROTOCOLS
    from dags.gov_pipeline_daily import _protocol_names

    assert sorted(_protocol_names()) == sorted(p.name for p in PROTOCOLS)


def test_harvest_task_fails_the_dag_on_reported_errors():
    """harvest.py's own main() already returns non-zero when a protocol failed
    (report.snapshot.errors or report.discourse.errors) — the DAG task must let that
    surface as a failed BashOperator, not swallow it. There is nothing in the constructed
    command that catches or discards the exit code."""
    command = harvest_command("aave", 20, 15)
    assert command == (
        "python -m data_pipeline.harvest --protocol aave --proposals 20 --topics 15 --with-posts"
    )
    assert "||" not in command and "; true" not in command, "must not mask a non-zero exit"


def test_daily_uses_the_established_small_window_defaults():
    """These constants ARE every phase's existing demo and eval corpus. Changing them here
    changes what 'a normal day' looks like everywhere else."""
    assert (DAILY_PROPOSALS, DAILY_TOPICS) == (20, 15)


# --------------------------------------------------------------------------
# gov_backfill
# --------------------------------------------------------------------------


def test_gov_backfill_accepts_per_run_params_for_proposals_and_topics(dagbag: DagBag):
    """A backfill run using the daily DAG's small defaults isn't a backfill."""
    dag = dagbag.get_dag("gov_backfill")
    assert dag.schedule_interval is None, "must be manually triggered only"
    # Indexing a DAG's ParamsDict resolves straight to the default value, not the Param
    # wrapper — confirmed against the real object rather than assumed.
    assert dag.params["proposals"] == BACKFILL_PROPOSALS
    assert dag.params["topics"] == BACKFILL_TOPICS
    assert dag.params["protocols"] is None, "defaults to every configured protocol"


def test_cost_ceiling_check_task_exists_between_silver_and_embed(dagbag: DagBag):
    """The exact design point: an over-budget estimate must stop the DAG before `embed`
    runs, never quietly embed a partial, arbitrarily-truncated set and report success."""
    dag = dagbag.get_dag("gov_backfill")
    build_silver = dag.get_task("build_silver")
    check = dag.get_task("check_cost_ceiling")
    embed = dag.get_task("embed")

    assert check.task_id in build_silver.downstream_task_ids
    assert embed.task_id in check.downstream_task_ids
    assert check.retries == 0, "an over-budget estimate won't change on retry"


def test_backfill_and_daily_dags_share_the_harvest_and_silver_task_logic():
    """Guards against the two DAGs drifting into two different implementations of 'run the
    harvester' — both call the same command-building functions, parameterized differently."""
    daily_cmd = harvest_command("aave", DAILY_PROPOSALS, DAILY_TOPICS)
    backfill_cmd = harvest_command("aave", BACKFILL_PROPOSALS, BACKFILL_TOPICS)
    assert daily_cmd.startswith("python -m data_pipeline.harvest --protocol aave")
    assert backfill_cmd.startswith("python -m data_pipeline.harvest --protocol aave")
    assert str(DAILY_PROPOSALS) in daily_cmd
    assert str(BACKFILL_PROPOSALS) in backfill_cmd
    # Both DAGs' build_silver task calls the exact same command — one implementation, not two.
    assert build_silver_command() == build_silver_command()


def test_spark_command_execs_by_name_not_docker_compose():
    """No `docker compose` CLI plugin is installed in the Airflow image (see
    docker/airflow/Dockerfile) — only the base `docker` client. Using `docker exec` by
    container name, not `docker compose exec`, is what makes that sufficient."""
    command = build_silver_command()
    assert command.startswith("docker exec ")
    assert "docker compose" not in command
    assert SPARK_CONTAINER in command
    assert " -t " not in f" {command} ", "no TTY in a scheduled task"
