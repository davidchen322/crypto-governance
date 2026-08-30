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

from data_pipeline.transformation.build_silver import (  # noqa: E402
    ensure_columns,
    optional_col,
    optional_struct_field,
)

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

    # The general safety net: ensure_columns() must fix EVERY absent column a target list
    # names, not just the one field a hand-written fix happened to cover. This is exactly
    # the gap the first version of this fix left open — CI broke a second time on `author`,
    # a field nothing here explicitly wrapped. Proven against three simultaneously-missing
    # columns of different target types, not just one.
    bare = spark.read.json(spark.sparkContext.parallelize(['{"id": "0xddd"}']))
    target_columns = ["id", "author", "link", "choices"]
    completed = ensure_columns(bare, target_columns).select(*target_columns).collect()
    check(
        "ensure_columns() adds every absent column so the select() it guards cannot raise",
        completed[0]["id"] == "0xddd"
        and completed[0]["author"] is None
        and completed[0]["link"] is None
        and completed[0]["choices"] is None,
    )

    # The third round: `slug` broke CI, not discussion/author — a top-level Discourse
    # topic field the first two rounds never touched, since they only covered
    # build_proposals(). Same fixture shape, same fix, different function.
    topic = spark.read.json(
        spark.sparkContext.parallelize(
            ['{"id": 1, "title": "T1", "post_stream": {"posts": [{"id": 10, "cooked": "hi"}]}}']
        )
    )
    check("the fixture reproduces the bug: 'slug' is absent", "slug" not in topic.columns)
    slug_fixed = topic.withColumn("topic_slug", optional_col(topic, "slug")).collect()
    check("optional_col() on 'slug' does not raise", slug_fixed[0]["topic_slug"] is None)

    # And the nested case: a field inside post_stream.posts[] is exposed to the identical
    # gap one level deeper. optional_struct_field must inspect the EXPLODED element's own
    # schema, not post_stream's (which has only one field, `posts`, an array).
    exploded = topic.withColumn("post", F.explode("post_stream.posts"))
    check(
        "the fixture reproduces the nested bug: 'post_number' is absent from the post struct",
        "post_number" not in exploded.schema["post"].dataType.names,
    )
    nested_raised = False
    try:
        exploded.withColumn("post_number", F.col("post.post_number")).collect()
    except Exception:
        nested_raised = True
    check("F.col() on an absent nested field does raise", nested_raised)
    nested_fixed = exploded.withColumn(
        "post_number", optional_struct_field(exploded, "post", "post_number")
    ).collect()
    check(
        "optional_struct_field() does not raise on an absent nested field",
        nested_fixed[0]["post_number"] is None,
    )
    present_nested = spark.read.json(
        spark.sparkContext.parallelize(
            ['{"id": 2, "post_stream": {"posts": [{"id": 20, "post_number": 3}]}}']
        )
    ).withColumn("post", F.explode("post_stream.posts"))
    kept_nested = present_nested.withColumn(
        "post_number", optional_struct_field(present_nested, "post", "post_number")
    ).collect()
    check(
        "optional_struct_field() passes a present nested field through unchanged",
        kept_nested[0]["post_number"] == 3,
    )

    # The structural case one level up: `post_stream` itself absent from the WHOLE batch —
    # measured to be exactly what Phase 2's own integration-test fixtures produce
    # ({"id": 12345, "title": "Temp check"}, {"id": 999} — see
    # tests/integration/test_phase2_bronze.py), which share the same bronze bucket Phase 3
    # reads. Referencing `post_stream.posts` at all when no object anywhere in the batch
    # ever mentions `post_stream` raises at plan-construction time — build_posts() branches
    # on `"post_stream" in raw.columns` before ever writing that reference, exactly the
    # pattern proven here in isolation.
    fixture_shaped = spark.read.json(
        spark.sparkContext.parallelize(['{"id": 12345, "title": "Temp check"}', '{"id": 999}'])
    )
    check(
        "the fixture matches Phase 2's real payloads: 'post_stream' is absent",
        "post_stream" not in fixture_shaped.columns,
    )
    branch_raised = False
    try:
        fixture_shaped.filter(F.col("post_stream.posts").isNotNull()).collect()
    except Exception:
        branch_raised = True
    check(
        "referencing post_stream.posts when the batch never mentions it does raise",
        branch_raised,
    )
    # The guard build_posts() actually takes: check presence before ever building the
    # reference, rather than trying to catch the exception after the fact.
    safe_empty = (
        fixture_shaped.filter(F.lit(False)) if "post_stream" not in fixture_shaped.columns else None
    )
    check(
        "the presence check lets code route around the reference entirely, with zero rows",
        safe_empty is not None and safe_empty.count() == 0,
    )

    spark.stop()
    if failures:
        print(f"\n{len(failures)} assertion(s) failed", file=sys.stderr)
        return 1
    print("\nPASS — optional_col() reproduces and fixes the CI failure deterministically")
    return 0


if __name__ == "__main__":
    sys.exit(main())
