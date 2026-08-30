"""Deterministic self-test of `optional_col`, run inside the Spark container.

    spark-submit /opt/app/tests/spark/optional_column_selftest.py

Why this exists separately from the integration tests: reproducing the actual bug requires
a batch of live-harvested bronze objects where a field is null in *every* row, which depends
on what Snapshot happens to be serving when CI runs. That is not a test, it is luck — this
constructs the exact schema-inference gap deterministically (a JSON batch that never
mentions `discussion` at all) so the mechanism is verified whether or not a real harvest
ever reproduces it again.

Exits non-zero on the first failed assertion, matching tests/spark/scd2_selftest.py.
"""

from __future__ import annotations

import sys

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

sys.path.insert(0, "/opt/app")

from data_pipeline.transformation.build_silver import optional_col  # noqa: E402

# No row in this batch mentions "discussion" at all — the exact shape that broke CI, where
# a freshly-harvested set of proposals happened to share no linked forum discussion yet.
# Spark's JSON schema inference omits a field entirely under this condition; it does not
# merely infer it as an all-null column.
NO_DISCUSSION_FIELD = [
    '{"id": "0xaaa", "title": "First"}',
    '{"id": "0xbbb", "title": "Second"}',
]

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    if condition:
        print(f"  ok  {label}")
    else:
        failures.append(label)
        print(f" FAIL {label}", file=sys.stderr)


def main() -> int:
    spark = SparkSession.builder.appName("optional-column-selftest").getOrCreate()
    spark.sparkContext.setLogLevel("WARN")

    raw = spark.read.json(spark.sparkContext.parallelize(NO_DISCUSSION_FIELD))

    check(
        "the fixture actually reproduces the bug: 'discussion' is absent from the schema",
        "discussion" not in raw.columns,
    )

    # The regression itself: a naive F.col() reference on a genuinely absent column raises
    # AnalysisException rather than returning nulls — this is what broke the whole build.
    raised = False
    try:
        raw.withColumn("discussion_url", F.col("discussion")).collect()
    except Exception:
        raised = True
    check("F.col() on a genuinely absent column does raise (confirms the bug is real)", raised)

    # The fix: optional_col() must not raise, and must produce nulls rather than data.
    fixed = raw.withColumn("discussion_url", optional_col(raw, "discussion")).collect()
    check("optional_col() does not raise on an absent column", True)
    check(
        "optional_col() produces NULL for every row, not a crash",
        all(r["discussion_url"] is None for r in fixed),
    )

    # And the non-regression: a field that IS present must still come through unchanged —
    # optional_col must not silently null out data that was actually there.
    present = spark.read.json(
        spark.sparkContext.parallelize(['{"id": "0xccc", "discussion": "https://forum/t/1"}'])
    )
    kept = present.withColumn("discussion_url", optional_col(present, "discussion")).collect()
    check(
        "optional_col() passes a present column through unchanged",
        kept[0]["discussion_url"] == "https://forum/t/1",
    )

    spark.stop()
    if failures:
        print(f"\n{len(failures)} assertion(s) failed", file=sys.stderr)
        return 1
    print("\nPASS — optional_col() reproduces and fixes the CI failure deterministically")
    return 0


if __name__ == "__main__":
    sys.exit(main())
