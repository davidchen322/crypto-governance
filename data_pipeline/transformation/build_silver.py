"""Build the SCD2 silver tables from the content-addressed bronze layer.

    docker compose exec spark spark-submit /opt/app/data_pipeline/transformation/build_silver.py

Because bronze is content-addressed and append-only, silver is a **pure function of
bronze** — a fold over immutable objects ordered by first observation, not a diff against
a live API. Two consequences the design leans on:

  * The build is deterministic. Running it twice over unchanged bronze produces byte-identical
    rows, which is what makes the idempotency guarantee provable rather than hopeful.
  * Ordering comes from object modification time, exposed by Spark's `_metadata` column.
    Bronze never overwrites (an existing key is skipped), so an object's mtime *is* the
    moment that content was first observed.

The write is a guarded MERGE, and the guard compares the WHOLE row — see `merge()` for the
two failure modes it sits between. Briefly: an unguarded MERGE rewrites every record on
every run and ruins the snapshot log; a guard narrowed to the SCD2 validity columns misses
changes to derived columns and silently serves stale data through a run that looks
successful.

Known limitation, stated plainly: content addressing cannot represent a revert. If a
document goes A -> B -> A, the third state hashes to a key that already exists, so no
bronze object is written and silver shows A -> B with B current. Reverts are rare in
governance text and the alternative — writing a bronze object per observation — would
trade this for unbounded storage growth.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

sys.path.insert(0, "/opt/app")

from config.protocols import PROTOCOLS  # noqa: E402

BUCKET = "warehouse"
DDL_PATH = Path("/opt/app/storage/iceberg_tables.sql")

SPACE_TO_PROTOCOL = {p.snapshot_space: p.name for p in PROTOCOLS}
HOST_TO_PROTOCOL = {p.discourse_host: p.name for p in PROTOCOLS}

# Discourse serves post bodies as HTML. Embeddings want prose, so a stripped copy rides
# alongside the verbatim original — never instead of it.
#
# Block-level tags become newlines BEFORE tags are stripped. An earlier version collapsed
# all whitespace including newlines, which produced text that was correct but structurally
# flat, and Phase 4's chunker splits on markdown headings — which need line starts to
# exist. Measured consequence: zero of 328 posts contained a single newline, so heading
# splitting could never fire, and one 2,123-character post whose text began with "#" was
# read as a single heading with an empty body and dropped from silver entirely.
HTML_BLOCK_END = r"(?i)</(p|div|li|ul|ol|h[1-6]|blockquote|tr|table|pre)>|<br\s*/?>"
HTML_TAG = r"<[^>]+>"

# Discourse emits entity-encoded punctuation in `cooked`. Left undecoded these embed as
# literal noise — 52 posts in the current corpus carry at least one. `&amp;` is decoded
# last so `&amp;lt;` resolves to `&lt;` rather than being double-decoded to `<`.
HTML_ENTITIES = [
    (r"&nbsp;", " "),
    (r"&lt;", "<"),
    (r"&gt;", ">"),
    (r"&quot;", '"'),
    (r"&#39;", "'"),
    (r"&hellip;", "…"),
    (r"&mdash;", "—"),
    (r"&ndash;", "–"),
    (r"&amp;", "&"),
]


def protocol_lookup(mapping: dict[str, str], column):
    """Build a CASE expression from a Python dict — small enough that a broadcast join
    would cost more than it saves."""
    expr = F.lit("unknown")
    for key, name in mapping.items():
        expr = F.when(column == key, F.lit(name)).otherwise(expr)
    return expr


def apply_ddl(spark: SparkSession) -> None:
    """Execute storage/iceberg_tables.sql so the schema has exactly one home."""
    statements = [s.strip() for s in DDL_PATH.read_text().split(";") if s.strip()]
    for statement in statements:
        spark.sql(statement)


def drop_unconfigured(df: DataFrame, label: str) -> DataFrame:
    """Keep only spaces/hosts declared in config/protocols.py.

    Bronze is a landing zone and holds more than silver should: the integration suite
    writes `space=probe-*` fixtures into the same bucket, and a future harvest of an
    unconfigured space would land here too. Filtering is reported rather than silent —
    a space quietly missing from silver looks identical to a space with no activity.
    """
    kept = df.filter(F.col("protocol_name") != "unknown")
    dropped = df.count() - kept.count()
    if dropped:
        print(f"  dropped {dropped} {label} rows from unconfigured spaces")
    return kept


def add_validity_windows(df: DataFrame, keys: list[str]) -> DataFrame:
    """Turn a stream of observations into SCD2 rows.

    `valid_to` is the next observation's timestamp for that entity, and NULL for the
    newest — so "the record as of T" is an ordinary predicate rather than a table-snapshot
    lookup that compaction can destroy.
    """
    # One row per (entity, record_hash). Content addressing already guarantees this, but
    # asserting it here keeps a malformed bronze object from silently duplicating a version.
    dedupe = Window.partitionBy(*keys, "record_hash").orderBy("observed_at")
    df = df.withColumn("_rn", F.row_number().over(dedupe)).filter(F.col("_rn") == 1).drop("_rn")

    timeline = Window.partitionBy(*keys).orderBy("observed_at")
    return (
        df.withColumn("valid_from", F.col("observed_at"))
        .withColumn("valid_to", F.lead("observed_at").over(timeline))
        .withColumn("is_current", F.lead("observed_at").over(timeline).isNull())
    )


# --------------------------------------------------------------------------
# Snapshot proposals
# --------------------------------------------------------------------------

PROPOSAL_COLUMNS = [
    "proposal_id",
    "protocol_name",
    "space_id",
    "source",
    "record_hash",
    "content_hash",
    "title",
    "body",
    "discussion_url",
    "proposal_state",
    "proposal_type",
    "author",
    "ipfs_cid",
    "link",
    "quorum",
    "scores_total",
    "vote_count",
    "choices",
    "scores",
    "voting_start",
    "voting_end",
    "proposal_created",
    "source_key",
    "observed_at",
    "valid_from",
    "valid_to",
    "is_current",
]


def build_proposals(spark: SparkSession) -> DataFrame:
    raw = (
        spark.read.option("recursiveFileLookup", "true")
        .json(f"s3a://{BUCKET}/snapshot/")
        .withColumn("source_key", F.col("_metadata.file_path"))
        .withColumn("observed_at", F.col("_metadata.file_modification_time"))
    )

    # `space=` sits in the key rather than the payload, so the path carries it. The
    # payload's own space.id would work too; the path is cheaper and always present.
    space = F.regexp_extract(F.col("source_key"), r"space=([^/]+)/", 1)

    typed = (
        raw.withColumn("space_id", space)
        .withColumn("protocol_name", protocol_lookup(SPACE_TO_PROTOCOL, space))
        .withColumn("proposal_id", F.col("id"))
        .withColumn("source", F.lit("snapshot"))
        .withColumn("discussion_url", F.col("discussion"))
        .withColumn("proposal_state", F.col("state"))
        .withColumn("proposal_type", F.col("type"))
        .withColumn("ipfs_cid", F.col("ipfs"))
        .withColumn("vote_count", F.col("votes").cast("bigint"))
        .withColumn("quorum", F.col("quorum").cast("double"))
        .withColumn("scores_total", F.col("scores_total").cast("double"))
        .withColumn("scores", F.col("scores").cast("array<double>"))
        .withColumn("voting_start", F.to_timestamp(F.from_unixtime(F.col("start"))))
        .withColumn("voting_end", F.to_timestamp(F.from_unixtime(F.col("end"))))
        .withColumn("proposal_created", F.to_timestamp(F.from_unixtime(F.col("created"))))
    )

    # content_hash covers text only. A proposal whose vote tally moved is a new *record*
    # but not new *text*, so Phase 4 must not re-embed it.
    hashed = typed.withColumn(
        "content_hash",
        F.sha2(
            F.concat_ws(
                "\u0000",
                F.coalesce(F.col("title"), F.lit("")),
                F.coalesce(F.col("body"), F.lit("")),
            ),
            256,
        ),
    ).withColumn(
        "record_hash",
        F.sha2(
            F.concat_ws(
                "\u0000",
                F.coalesce(F.col("content_hash"), F.lit("")),
                F.coalesce(F.col("proposal_state"), F.lit("")),
                F.coalesce(F.col("scores_total").cast("string"), F.lit("")),
                F.coalesce(F.col("vote_count").cast("string"), F.lit("")),
                F.coalesce(F.col("discussion_url"), F.lit("")),
            ),
            256,
        ),
    )

    configured = drop_unconfigured(hashed, "proposal")
    return add_validity_windows(configured, ["proposal_id"]).select(*PROPOSAL_COLUMNS)


# --------------------------------------------------------------------------
# Discourse posts
# --------------------------------------------------------------------------

POST_COLUMNS = [
    "forum_host",
    "protocol_name",
    "topic_id",
    "post_id",
    "post_number",
    "record_hash",
    "content_hash",
    "topic_title",
    "topic_slug",
    "username",
    "body_html",
    "body_text",
    "post_created_at",
    "post_updated_at",
    "reply_count",
    "source_key",
    "observed_at",
    "valid_from",
    "valid_to",
    "is_current",
]


def build_posts(spark: SparkSession) -> DataFrame:
    raw = (
        spark.read.option("recursiveFileLookup", "true")
        .json(f"s3a://{BUCKET}/discourse/")
        .withColumn("source_key", F.col("_metadata.file_path"))
        .withColumn("observed_at", F.col("_metadata.file_modification_time"))
    )

    host = F.regexp_extract(F.col("source_key"), r"space=([^/]+)/", 1)

    # A topic object without post_stream came from a harvest run without --with-posts.
    # Dropping it silently would leave a gap that looks like the forum went quiet, so it
    # is filtered explicitly and counted in the run summary.
    topics = (
        raw.withColumn("forum_host", host)
        .withColumn("protocol_name", protocol_lookup(HOST_TO_PROTOCOL, host))
        .withColumn("topic_id", F.col("id").cast("bigint"))
        .withColumn("topic_title", F.col("title"))
        .withColumn("topic_slug", F.col("slug"))
        .filter(F.col("post_stream.posts").isNotNull())
    )

    posts = (
        topics.withColumn("post", F.explode("post_stream.posts"))
        .withColumn("post_id", F.col("post.id").cast("bigint"))
        .withColumn("post_number", F.col("post.post_number").cast("int"))
        .withColumn("username", F.col("post.username"))
        .withColumn("body_html", F.col("post.cooked"))
        .withColumn("post_created_at", F.to_timestamp(F.col("post.created_at")))
        .withColumn("post_updated_at", F.to_timestamp(F.col("post.updated_at")))
        .withColumn("reply_count", F.col("post.reply_count").cast("int"))
    )

    # Order matters: block ends become newlines, then remaining tags go, then entities are
    # decoded, then only *horizontal* whitespace is collapsed so paragraph breaks survive.
    text = F.regexp_replace(F.coalesce(F.col("body_html"), F.lit("")), HTML_BLOCK_END, "\n")
    text = F.regexp_replace(text, HTML_TAG, " ")
    for entity, char in HTML_ENTITIES:
        text = F.regexp_replace(text, entity, char)
    text = F.regexp_replace(text, r"[ \t]+", " ")
    text = F.regexp_replace(text, r" *\n *", "\n")
    text = F.regexp_replace(text, r"\n{3,}", "\n\n")

    cleaned = posts.withColumn("body_text", F.trim(text))

    hashed = cleaned.withColumn(
        "content_hash", F.sha2(F.coalesce(F.col("body_html"), F.lit("")), 256)
    ).withColumn(
        "record_hash",
        F.sha2(
            F.concat_ws(
                "\u0000",
                F.col("content_hash"),
                F.coalesce(F.col("post_updated_at").cast("string"), F.lit("")),
                F.coalesce(F.col("topic_title"), F.lit("")),
            ),
            256,
        ),
    )

    return add_validity_windows(hashed, ["forum_host", "topic_id", "post_id"]).select(*POST_COLUMNS)


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


def merge(spark: SparkSession, staged: DataFrame, table: str, keys: list[str]) -> None:
    """Guarded MERGE, comparing the whole row.

    Two failure modes this navigates between, both observed:

    1. An UNGUARDED merge commits an Iceberg `overwrite` snapshot rewriting every record
       even when nothing changed, because copy-on-write MERGE rewrites each data file
       holding a matched row regardless. The data stays correct; the snapshot log becomes
       useless, and "nothing changed" reads identically to "everything changed".

    2. A guard that compares only the SCD2 validity columns misses changes to derived
       columns. When the HTML-to-text pipeline was rewritten, `record_hash` did not move —
       it is computed from the source `body_html`, not the derived `body_text` — so the
       narrow guard reported "no changes" and silver silently kept the stale text through
       a run that looked entirely successful.

    So the comparison is over every non-key column. `record_hash` still governs SCD2
    identity; this governs whether the stored row is stale.
    """
    view = f"staged_{table.split('.')[-1]}"
    staged.createOrReplaceTempView(view)
    key_cols = [*keys, "record_hash"]
    value_cols = [c for c in staged.columns if c not in key_cols]
    # `<=>` is null-safe equality; plain `<>` would treat NULL != NULL as unknown and
    # silently drop those rows from the change count.
    differs = " OR ".join(f"NOT (t.{c} <=> s.{c})" for c in value_cols)
    existing = spark.table(table)

    new_rows = staged.join(existing, key_cols, "left_anti").count()
    changed = staged.alias("s").join(existing.alias("t"), key_cols).filter(differs).count()

    if new_rows == 0 and changed == 0:
        print(f"  {table}: no changes, MERGE skipped")
        return

    on = " AND ".join(f"t.{k} = s.{k}" for k in key_cols)
    spark.sql(f"""
        MERGE INTO {table} t
        USING {view} s
        ON {on}
        WHEN MATCHED AND ({differs}) THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)
    print(f"  {table}: {new_rows} new row(s), {changed} updated")


def summarize(spark: SparkSession, table: str, entity: str) -> None:
    row = spark.sql(f"""
        SELECT count(*) AS versions,
               count(DISTINCT {entity}) AS entities,
               sum(CASE WHEN is_current THEN 1 ELSE 0 END) AS current_rows
        FROM {table}
    """).collect()[0]
    print(
        f"  {table:<34} {row['versions']:5d} versions  "
        f"{row['entities']:5d} entities  {row['current_rows']:5d} current"
    )


def main() -> int:
    spark = SparkSession.builder.appName("build-silver").getOrCreate()
    spark.sparkContext.setLogLevel("WARN")

    print("applying DDL from storage/iceberg_tables.sql")
    apply_ddl(spark)

    print("reading bronze and computing validity windows")
    proposals = build_proposals(spark)
    posts = build_posts(spark)

    merge(spark, proposals, "gov.silver.proposal_versions", ["proposal_id"])
    merge(spark, posts, "gov.silver.forum_posts", ["forum_host", "topic_id", "post_id"])

    print("\nsilver:")
    summarize(spark, "gov.silver.proposal_versions", "proposal_id")
    summarize(spark, "gov.silver.forum_posts", "post_id")

    spark.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
