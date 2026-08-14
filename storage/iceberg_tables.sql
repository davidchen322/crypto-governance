-- Silver layer: slowly-changing-dimension Type 2 over the bronze content-addressed store.
--
-- Applied by data_pipeline/transformation/build_silver.py, which reads this file so the
-- DDL has exactly one home.
--
-- WHY SCD2 RATHER THAN ICEBERG SNAPSHOTS
-- Iceberg time travel answers "what did this *table* look like at snapshot X" — not
-- "what did proposal ABC say on March 3rd". Relying on table snapshots for document
-- history is fragile: `expire_snapshots` exists because metadata accumulates, and its
-- default retention is five days, so routine housekeeping becomes a data-loss event.
-- Validity windows live in the rows instead. Iceberg then does what it is genuinely good
-- at — ACID commits, pipeline rollback, schema evolution — without being load-bearing for
-- the archive.
--
-- TWO HASHES, DELIBERATELY
--   record_hash   sha256 over the full observed record. Drives SCD2 versioning, so a
--                 vote tally moving produces a new row and the trajectory is preserved.
--   content_hash  sha256 over the *text only* (title + body, or a post's body). Drives
--                 re-embedding in Phase 4. Without the split, every vote that arrives on
--                 an active proposal would invalidate its embeddings for no reason.

CREATE NAMESPACE IF NOT EXISTS gov.silver;

-- --------------------------------------------------------------------------
-- Snapshot proposals
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gov.silver.proposal_versions (
    proposal_id       STRING  NOT NULL,
    protocol_name     STRING  NOT NULL,
    space_id          STRING  NOT NULL,
    source            STRING  NOT NULL,

    record_hash       STRING  NOT NULL,   -- versioning identity
    content_hash      STRING  NOT NULL,   -- text identity, joins to embeddings

    title             STRING,
    body              STRING,
    discussion_url    STRING,             -- the join key to the forum
    proposal_state    STRING,             -- pending | active | closed
    proposal_type     STRING,
    author            STRING,
    ipfs_cid          STRING,
    link              STRING,

    quorum            DOUBLE,
    scores_total      DOUBLE,
    vote_count        BIGINT,
    choices           ARRAY<STRING>,
    scores            ARRAY<DOUBLE>,

    voting_start      TIMESTAMP,
    voting_end        TIMESTAMP,
    proposal_created  TIMESTAMP,

    source_key        STRING,             -- bronze object this row was derived from
    observed_at       TIMESTAMP NOT NULL, -- when the harvester first saw this content
    valid_from        TIMESTAMP NOT NULL,
    valid_to          TIMESTAMP,          -- NULL = still current
    is_current        BOOLEAN   NOT NULL
)
USING iceberg
PARTITIONED BY (protocol_name)
TBLPROPERTIES (
    'format-version' = '2',
    'write.parquet.compression-codec' = 'zstd'
);

-- --------------------------------------------------------------------------
-- Discourse forum posts
--
-- Post-level rather than topic-level: a thread with 40 replies where one post is edited
-- should produce one new version, not a new copy of the whole thread. It is also the
-- grain Phase 4 wants to chunk and embed.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gov.silver.forum_posts (
    forum_host        STRING  NOT NULL,
    protocol_name     STRING  NOT NULL,
    topic_id          BIGINT  NOT NULL,
    post_id           BIGINT  NOT NULL,
    post_number       INT,

    record_hash       STRING  NOT NULL,
    content_hash      STRING  NOT NULL,

    topic_title       STRING,
    topic_slug        STRING,
    username          STRING,
    body_html         STRING,             -- Discourse `cooked`, kept verbatim
    body_text         STRING,             -- tags stripped, for embedding

    post_created_at   TIMESTAMP,
    post_updated_at   TIMESTAMP,
    reply_count       INT,

    source_key        STRING,
    observed_at       TIMESTAMP NOT NULL,
    valid_from        TIMESTAMP NOT NULL,
    valid_to          TIMESTAMP,
    is_current        BOOLEAN   NOT NULL
)
USING iceberg
PARTITIONED BY (protocol_name)
TBLPROPERTIES (
    'format-version' = '2',
    'write.parquet.compression-codec' = 'zstd'
);
