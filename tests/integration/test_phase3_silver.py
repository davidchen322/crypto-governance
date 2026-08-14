"""Phase 3 acceptance: SCD2 silver tables built from bronze.

Exit criteria from the plan: idempotent across three consecutive runs, and a point-in-time
query returns the correct historical version.

These assert on the *rows*, not on the job's own report — a build that prints "0 changed"
while quietly rewriting the table would pass a self-report check.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.integration.conftest import REPO, run_spark_sql

pytestmark = pytest.mark.integration

PROPOSALS = "gov.silver.proposal_versions"
POSTS = "gov.silver.forum_posts"


def run_silver_build(timeout: int = 900) -> str:
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "spark",
            "spark-submit",
            "--master",
            "local[*]",
            "/opt/app/data_pipeline/transformation/build_silver.py",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"build_silver failed (exit {proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout[-3000:]}\n"
            f"--- stderr ---\n{proc.stderr[-4000:]}"
        )
    return proc.stdout


def scalar(sql: str) -> str:
    """Run a single-value query and return it as text."""
    return run_spark_sql(f"{sql};").strip().splitlines()[-1].strip()


def rows_json(sql: str) -> list[dict]:
    out = run_spark_sql(f"SELECT to_json(struct(*)) FROM ({sql});")
    parsed = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            parsed.append(json.loads(line))
    return parsed


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


@pytest.mark.seed
def test_silver_build_succeeds():
    output = run_silver_build()
    assert "gov.silver.proposal_versions" in output
    assert "gov.silver.forum_posts" in output


def test_both_tables_are_populated():
    for table in (PROPOSALS, POSTS):
        count = int(scalar(f"SELECT count(*) FROM {table}"))
        assert count > 0, f"{table} is empty — the build produced no rows"


def test_every_entity_has_exactly_one_current_row():
    """The core SCD2 invariant. More than one current row means the timeline forked;
    zero means every version got closed out and the present is unrepresented."""
    bad = scalar(f"""
        SELECT count(*) FROM (
            SELECT proposal_id, sum(CASE WHEN is_current THEN 1 ELSE 0 END) AS c
            FROM {PROPOSALS} GROUP BY proposal_id HAVING c <> 1
        )
    """)
    assert bad == "0", f"{bad} proposals do not have exactly one current row"

    bad = scalar(f"""
        SELECT count(*) FROM (
            SELECT forum_host, topic_id, post_id,
                   sum(CASE WHEN is_current THEN 1 ELSE 0 END) AS c
            FROM {POSTS} GROUP BY forum_host, topic_id, post_id HAVING c <> 1
        )
    """)
    assert bad == "0", f"{bad} posts do not have exactly one current row"


def test_current_rows_are_open_ended():
    """is_current and a NULL valid_to must agree, or point-in-time queries and
    'give me the latest' queries disagree with each other."""
    for table in (PROPOSALS, POSTS):
        bad = scalar(f"""
            SELECT count(*) FROM {table}
            WHERE (is_current AND valid_to IS NOT NULL)
               OR (NOT is_current AND valid_to IS NULL)
        """)
        assert bad == "0", f"{table}: {bad} rows disagree between is_current and valid_to"


def test_validity_windows_do_not_overlap_or_gap():
    """Each version's valid_to must equal the next version's valid_from. A gap makes a
    point-in-time query return nothing; an overlap makes it return two rows."""
    bad = scalar(f"""
        SELECT count(*) FROM (
            SELECT valid_to,
                   lead(valid_from) OVER (PARTITION BY proposal_id ORDER BY valid_from) AS nxt
            FROM {PROPOSALS}
        ) WHERE nxt IS NOT NULL AND NOT (valid_to <=> nxt)
    """)
    assert bad == "0", f"{bad} proposal validity windows have a gap or overlap"


def test_no_duplicate_versions():
    """(entity, record_hash) must be unique — a repeated pair means the same observed
    state was written twice."""
    bad = scalar(f"""
        SELECT count(*) FROM (
            SELECT proposal_id, record_hash, count(*) AS c
            FROM {PROPOSALS} GROUP BY proposal_id, record_hash HAVING c > 1
        )
    """)
    assert bad == "0", f"{bad} duplicate (proposal_id, record_hash) pairs"


# --------------------------------------------------------------------------
# The two-hash split
# --------------------------------------------------------------------------


def test_content_hash_is_stable_when_only_votes_move():
    """The reason for two hashes. A proposal whose tally moved is a new *record* but not
    new *text* — if content_hash changed too, Phase 4 would re-embed for nothing."""
    rows = rows_json(f"""
        SELECT proposal_id,
               count(DISTINCT record_hash)  AS records,
               count(DISTINCT content_hash) AS contents
        FROM {PROPOSALS} GROUP BY proposal_id HAVING records > 1
    """)
    if not rows:
        pytest.skip("no multi-version proposals in the current bronze window")
    unchanged_text = [r for r in rows if int(r["contents"]) == 1]
    assert unchanged_text, (
        "every multi-version proposal also changed its text — expected at least one "
        "where only the tally moved"
    )


def test_content_hash_differs_across_different_proposals():
    distinct = scalar(f"SELECT count(DISTINCT content_hash) FROM {PROPOSALS}")
    entities = scalar(f"SELECT count(DISTINCT proposal_id) FROM {PROPOSALS}")
    assert int(distinct) > 1, "all proposals share a content_hash — hashing is broken"
    assert int(distinct) <= int(entities) + 50, "implausibly many distinct content hashes"


# --------------------------------------------------------------------------
# Point-in-time query — the Phase 3 demo
# --------------------------------------------------------------------------


def test_point_in_time_query_returns_exactly_one_row_per_entity():
    """'The proposal as of T' must be an ordinary predicate, and must resolve to a single
    row for every entity that existed at T."""
    bad = scalar(f"""
        SELECT count(*) FROM (
            SELECT proposal_id, count(*) AS c FROM {PROPOSALS}
            WHERE valid_from <= current_timestamp()
              AND (valid_to > current_timestamp() OR valid_to IS NULL)
            GROUP BY proposal_id HAVING c <> 1
        )
    """)
    assert bad == "0", f"{bad} proposals resolve to multiple rows at a single instant"


def test_point_in_time_before_first_observation_returns_nothing():
    count = scalar(f"""
        SELECT count(*) FROM {PROPOSALS}
        WHERE valid_from <= TIMESTAMP '2000-01-01 00:00:00'
          AND (valid_to > TIMESTAMP '2000-01-01 00:00:00' OR valid_to IS NULL)
    """)
    assert count == "0", "rows are visible before they were ever observed"


def test_earlier_version_is_recoverable_at_its_own_timestamp():
    """The actual demo: ask for a superseded version at a time inside its window and get
    that version back, not the current one."""
    rows = rows_json(f"""
        SELECT proposal_id, record_hash, valid_from, valid_to
        FROM {PROPOSALS} WHERE valid_to IS NOT NULL LIMIT 1
    """)
    if not rows:
        pytest.skip("no superseded versions in the current bronze window")
    row = rows[0]

    got = rows_json(f"""
        SELECT record_hash FROM {PROPOSALS}
        WHERE proposal_id = '{row["proposal_id"]}'
          AND valid_from <= TIMESTAMP '{row["valid_from"]}'
          AND (valid_to > TIMESTAMP '{row["valid_from"]}' OR valid_to IS NULL)
    """)
    assert len(got) == 1
    assert got[0]["record_hash"] == row["record_hash"], (
        "point-in-time query returned a different version than the one whose window "
        "contains the timestamp"
    )


# --------------------------------------------------------------------------
# Idempotency — the Phase 3 exit criterion
# --------------------------------------------------------------------------


@pytest.mark.slow
def test_rebuilding_twice_changes_nothing():
    """Asserted on row counts and a content fingerprint, not on the job's own report."""

    def fingerprint() -> tuple[str, str]:
        return (
            scalar(f"SELECT count(*) FROM {PROPOSALS}"),
            scalar(
                f"SELECT sha2(concat_ws(',', sort_array(collect_list(record_hash))), 256) "
                f"FROM {PROPOSALS}"
            ),
        )

    before = fingerprint()
    run_silver_build()
    after = fingerprint()
    assert before == after, f"a no-op rebuild changed silver: {before} -> {after}"


# --------------------------------------------------------------------------
# Deterministic SCD2 mechanism check
# --------------------------------------------------------------------------


def test_scd2_window_logic_selftest():
    """Runs tests/spark/scd2_selftest.py against a handcrafted three-version timeline.

    The integration assertions above verify invariants on harvested data, but harvested
    data only contains multiple versions if something upstream actually changed while the
    harvester was watching. This proves the mechanism regardless.
    """
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "spark",
            "spark-submit",
            "--master",
            "local[*]",
            "/opt/app/tests/spark/scd2_selftest.py",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"SCD2 self-test failed\n--- stdout ---\n{proc.stdout[-3000:]}\n"
        f"--- stderr ---\n{proc.stderr[-2000:]}"
    )
    assert "PASS" in proc.stdout
