"""Deterministic self-test of the SCD2 window logic, run inside the Spark container.

    spark-submit /opt/app/tests/spark/scd2_selftest.py

Why this exists separately from the integration tests: proving `add_validity_windows`
correct against *harvested* data requires a document to actually change upstream, which
depends on someone editing a forum post or a vote landing while the harvester is watching.
That is not a test, it is a wait. This constructs a known three-version timeline and
asserts the exact windows, so the mechanism is verified whether or not the internet
cooperated today.

Exits non-zero on the first failed assertion; the integration suite shells out to it.
"""

from __future__ import annotations

import sys
from datetime import datetime

from pyspark.sql import SparkSession
from pyspark.sql.types import StringType, StructField, StructType, TimestampType

sys.path.insert(0, "/opt/app")

from data_pipeline.transformation.build_silver import add_validity_windows  # noqa: E402

T1 = datetime(2026, 8, 1, 12, 0, 0)
T2 = datetime(2026, 8, 2, 12, 0, 0)
T3 = datetime(2026, 8, 3, 12, 0, 0)

SCHEMA = StructType(
    [
        StructField("entity_id", StringType(), False),
        StructField("record_hash", StringType(), False),
        StructField("observed_at", TimestampType(), False),
    ]
)

# A: three distinct versions observed in order.
# B: a single version — must come out open-ended, not closed.
# C: the same content seen twice — content addressing should make this impossible, but
#    the dedupe guard must collapse it rather than emit a zero-width window.
FIXTURE = [
    ("A", "hash-a1", T1),
    ("A", "hash-a2", T2),
    ("A", "hash-a3", T3),
    ("B", "hash-b1", T2),
    ("C", "hash-c1", T1),
    ("C", "hash-c1", T2),
]

failures: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual == expected:
        print(f"  ok  {label}")
    else:
        failures.append(f"{label}: expected {expected!r}, got {actual!r}")
        print(f" FAIL {label}: expected {expected!r}, got {actual!r}", file=sys.stderr)


def main() -> int:
    spark = SparkSession.builder.appName("scd2-selftest").getOrCreate()
    spark.sparkContext.setLogLevel("ERROR")

    df = spark.createDataFrame(FIXTURE, SCHEMA)
    out = add_validity_windows(df, ["entity_id"]).orderBy("entity_id", "valid_from")
    rows = {(r["entity_id"], r["record_hash"]): r for r in out.collect()}

    # --- A: a three-version timeline chains without gaps -------------------
    check("A has three versions", sum(1 for k in rows if k[0] == "A"), 3)
    check("A/v1 opens at T1", rows[("A", "hash-a1")]["valid_from"], T1)
    check("A/v1 closes when v2 arrives", rows[("A", "hash-a1")]["valid_to"], T2)
    check("A/v2 closes when v3 arrives", rows[("A", "hash-a2")]["valid_to"], T3)
    check("A/v3 is open-ended", rows[("A", "hash-a3")]["valid_to"], None)

    # --- exactly one current row per entity, and it is the newest ----------
    check("A/v1 is not current", rows[("A", "hash-a1")]["is_current"], False)
    check("A/v2 is not current", rows[("A", "hash-a2")]["is_current"], False)
    check("A/v3 is current", rows[("A", "hash-a3")]["is_current"], True)

    # --- B: a single observation must not be closed out --------------------
    check("B has one version", sum(1 for k in rows if k[0] == "B"), 1)
    check("B is open-ended", rows[("B", "hash-b1")]["valid_to"], None)
    check("B is current", rows[("B", "hash-b1")]["is_current"], True)

    # --- C: repeated identical content collapses to one row ----------------
    check("C deduplicates to one version", sum(1 for k in rows if k[0] == "C"), 1)
    check("C keeps the earliest observation", rows[("C", "hash-c1")]["valid_from"], T1)
    check("C stays open-ended", rows[("C", "hash-c1")]["valid_to"], None)

    # --- the point-in-time predicate resolves to the right version ---------
    out.createOrReplaceTempView("scd2")
    for at, expected in [(T1, "hash-a1"), (T2, "hash-a2"), (T3, "hash-a3")]:
        got = spark.sql(f"""
            SELECT record_hash FROM scd2
            WHERE entity_id = 'A'
              AND valid_from <= TIMESTAMP '{at}'
              AND (valid_to > TIMESTAMP '{at}' OR valid_to IS NULL)
        """).collect()
        check(f"as-of {at:%Y-%m-%d} resolves to one row", len(got), 1)
        check(f"as-of {at:%Y-%m-%d} returns {expected}", got[0]["record_hash"], expected)

    before = spark.sql("""
        SELECT count(*) AS c FROM scd2
        WHERE valid_from <= TIMESTAMP '2020-01-01 00:00:00'
          AND (valid_to > TIMESTAMP '2020-01-01 00:00:00' OR valid_to IS NULL)
    """).collect()[0]["c"]
    check("nothing is visible before it was observed", before, 0)

    spark.stop()
    if failures:
        print(f"\n{len(failures)} assertion(s) failed", file=sys.stderr)
        return 1
    print(f"\nPASS — {len(FIXTURE)} fixture rows, all SCD2 assertions hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
