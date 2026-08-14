# Phase 3 — Lakehouse: Build Results

**Status:** Complete · **Date:** 14 Aug 2026 · **Suite:** 11 passed, 2 skipped

A Spark job that folds the content-addressed bronze layer into SCD2 Iceberg tables, so
"what did this document say on date X" is an ordinary SQL predicate rather than a
table-snapshot lookup that compaction can destroy.

---

## Verdict

The Phase 3 exit criteria were *"idempotent across consecutive runs, and a point-in-time
query returns the correct historical version."*

| Check | Result |
| --- | --- |
| Both silver tables populate from bronze | **pass** — 125 proposal versions, 328 forum posts |
| Exactly one current row per entity | **pass** |
| `is_current` agrees with a NULL `valid_to` | **pass** |
| Validity windows chain with no gap or overlap | **pass** |
| No duplicate `(entity, record_hash)` pairs | **pass** |
| Point-in-time resolves to exactly one row per entity | **pass** |
| Nothing is visible before it was observed | **pass** |
| Rebuild over unchanged bronze changes nothing | **pass** |
| SCD2 window mechanism (21 assertions) | **pass** |
| Superseded version recoverable on *harvested* data | **skipped — no multi-version entity exists yet** |
| `content_hash` stable when only votes move, on harvested data | **skipped — same reason** |

The two skips are honest, not incidental. See *The gap in the demo* below.

---

## What was built

```
storage/iceberg_tables.sql                       SCD2 DDL, single source of truth
data_pipeline/transformation/build_silver.py     the Spark job
tests/spark/scd2_selftest.py                     deterministic mechanism check
tests/integration/test_phase3_silver.py          invariants on harvested data
```

```bash
make silver
```

### Silver contents

| Protocol | Proposal versions | Forum post versions | Topics |
| --- | --- | --- | --- |
| aave | 25 | 28 | 8 |
| arbitrum | 25 | 65 | 8 |
| ens | 25 | 59 | 8 |
| optimism | 25 | 81 | 8 |
| uniswap | 25 | 95 | 8 |
| **Total** | **125** | **328** | **40** |

---

## Design decisions

### Silver is a pure function of bronze

Because bronze is content-addressed and append-only, the build is a **fold over immutable
objects ordered by first observation** — not a diff against a live API. Two things follow:

- The build is deterministic, which is what makes the idempotency claim provable rather
  than hopeful.
- Ordering comes from object modification time via Spark's `_metadata` column. Bronze never
  overwrites (an existing key is skipped), so an object's mtime *is* the moment that content
  was first observed. No extra bookkeeping table, no watermark to drift.

### Two hashes, deliberately

| Hash | Covers | Drives |
| --- | --- | --- |
| `content_hash` | title + body only (or a post's body) | Re-embedding in Phase 4 |
| `record_hash` | content + state + tally + discussion link | SCD2 versioning |

Without the split, every vote arriving on an active proposal would invalidate that
proposal's embeddings even though not a word of its text changed. With it, vote movement
produces a new *row* while text identity stays stable, and Phase 4 joins on `content_hash`
to embed only what actually changed.

### Post-level grain for the forum

`forum_posts` is keyed on `(forum_host, topic_id, post_id)`, not per topic. A forty-reply
thread where one post is edited produces **one** new version rather than a fresh copy of the
whole thread. It is also the grain Phase 4 wants to chunk and embed.

`body_html` keeps Discourse's `cooked` output verbatim; `body_text` carries a tag-stripped
copy for embedding. The stripped version rides alongside the original, never instead of it.

### SCD2 rather than Iceberg snapshots

Restating the reason, because it is the whole point of the phase: Iceberg time travel
answers *"what did this table look like at snapshot X"*, not *"what did proposal ABC say on
March 3rd."* Relying on table snapshots for document history is fragile — `expire_snapshots`
exists because metadata accumulates, and its default retention is five days, so routine
housekeeping becomes a data-loss event.

Validity windows live in the rows. Iceberg then does what it is genuinely good at — ACID
commits, pipeline rollback, schema evolution — without being load-bearing for the archive.

---

## The finding that changed the implementation

The job's first docstring claimed that MERGE would make an unchanged re-run "write nothing,
so Iceberg's snapshot log records real deltas." The snapshot log falsified it:

```
operation   added-records
append      125          <- first build
overwrite   125          <- re-run over unchanged bronze
overwrite   125          <- and again
overwrite   125          <- and again
```

**Iceberg's copy-on-write MERGE rewrites every data file containing a matched row, whether
or not the row actually changes.** The data stayed byte-identical — so the row-level
idempotency test passed either way — but the table accumulated a full-rewrite snapshot per
run. That is precisely the noise that makes "nothing changed" indistinguishable from
"everything changed" when reading the history later.

**Fix:** guard the MERGE rather than trusting it to no-op. `merge()` now counts new rows
(anti-join on `(keys, record_hash)`) and closed-out rows (matched rows whose validity
window moved) *before* issuing the statement, and skips it when both are zero.

```
gov.silver.proposal_versions: no changes, MERGE skipped
gov.silver.forum_posts: no changes, MERGE skipped
```

Verified: after the guarded run, the snapshot log is unchanged at four entries.

This is worth dwelling on because the original test would never have caught it. Asserting
on row counts and content fingerprints proves the *data* is idempotent; it says nothing
about whether the table is quietly rewriting itself. The snapshot log was the only place
the problem was visible.

---

## The gap in the demo

The plan's Phase 3 demo was *"a SQL query showing a forum post that was edited — both
versions side by side."*

**No such row exists yet.** Every one of the 125 proposals and 328 posts is single-version:
`count(DISTINCT record_hash)` equals the row count exactly. A re-harvest over the same
window wrote zero new bronze objects — no vote landed and no post was edited while the
harvester was watching.

That is a data availability fact, not a defect, but it has a real consequence: the
invariant tests above would all pass on a table where the SCD2 logic never fired. Passing
them proves very little on its own.

So the mechanism is verified separately and deterministically. `tests/spark/scd2_selftest.py`
constructs a known timeline and asserts exact window boundaries:

- **A** — three versions observed in order
- **B** — a single version, which must come out open-ended rather than closed
- **C** — the same content seen twice, which must collapse to one row rather than emit a
  zero-width window

All 21 assertions hold, including the point-in-time predicate resolving to `hash-a1`,
`hash-a2` and `hash-a3` at their respective timestamps, and returning nothing at all for a
date before the first observation.

```
  ok  A/v1 closes when v2 arrives
  ok  A/v2 closes when v3 arrives
  ok  A/v3 is open-ended
  ok  C deduplicates to one version
  ok  as-of 2026-08-02 returns hash-a2
  ok  nothing is visible before it was observed
PASS — 6 fixture rows, all SCD2 assertions hold
```

The two skipped integration tests will start running on their own once a proposal's tally
moves or a forum post is edited between harvests. They skip rather than pass so the absence
stays visible.

---

## Deviations from the plan

**Spark reads bronze over `s3a://`, which needed jars the image lacked.** Iceberg's
`S3FileIO` only serves Iceberg tables; Spark's own file reader needs the `s3a` filesystem,
a separate stack. Added `hadoop-aws` 3.3.4 — pinned to the Hadoop version Spark 3.5.3
bundles, since a mismatch fails at class-load rather than at build — plus the AWS SDK v1
bundle it expects. It coexists with Iceberg's SDK v2 because they occupy different packages
(`com.amazonaws` vs `software.amazon.awssdk`).

The alternative was reading bronze through the driver with boto3, which at 17 MB would have
worked fine and skipped a 267 MB jar. It was rejected because it does not scale, and a
medallion architecture that cannot read its own object store is a diagram rather than a
lakehouse.

**Silver filters to configured protocols.** The first build ingested the integration
suite's `probe-*` fixtures — 22 entities from 20 real objects. Bronze is a landing zone and
legitimately holds more than silver should. The filter is now explicit and the drop is
**reported**, not silent:

```
dropped 5 proposal rows from unconfigured spaces
```

A space quietly missing from silver looks identical to a space with no activity, which is
exactly the class of bug that goes unnoticed for months.

---

## Known gaps

**Content addressing cannot represent a revert.** If a document goes A → B → A, the third
state hashes to a key that already exists, so no bronze object is written and silver shows
A → B with B current. Reverts are rare in governance text, and the alternative — one bronze
object per observation — trades this for unbounded storage growth. Worth knowing before
trusting the timeline as a complete audit record.

**HTML stripping is a regex.** `body_text` is produced by removing `<[^>]+>` and collapsing
whitespace. It handles Discourse's `cooked` output well enough for embedding but will mangle
a post containing a literal `<` in a code block. Phase 4 should check a sample before
trusting it.

**Post-level versioning depends on topic-level fetches.** A post's version only advances
when the *topic* object changes enough to produce a new bronze object. The Phase 2 projection
excludes read counters and view telemetry, so an edit does register — but a post edited in a
thread that is never re-fetched will not be noticed until the next harvest touches it.

**Timestamps come from object mtime, not the source.** `observed_at` is when the harvester
first saw the content, not when the author changed it. Discourse exposes real revision
history via its API; wiring that in would give true edit timestamps rather than observation
timestamps. Not needed for Phase 4, but it is the difference between "we noticed on Tuesday"
and "it was edited on Sunday."

**The suite is slow.** 12m49s, almost entirely JVM startup — each `spark-sql` probe pays a
fresh Spark session. A single session running all assertions would cut this by an order of
magnitude if it becomes a problem.

---

## Next

Phase 4 — chunking, embeddings and the evaluation harness. Silver already carries what it
needs: `content_hash` to embed only changed text, `valid_from`/`valid_to` to support
point-in-time retrieval, and `body_text` at post grain.

The one credential required arrives here. See [`api-keys.md`](api-keys.md), and run
`make check-openai` before the first backfill.
