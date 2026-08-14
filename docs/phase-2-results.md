# Phase 2 — Ingestion: Build Results

**Status:** Complete · **Date:** 14 Aug 2026 · **Exit criterion:** met at harvest 3

Snapshot and Discourse clients writing raw API responses into a content-addressed bronze
layer in MinIO. No AI, no Airflow — a plain CLI, per the plan's sequencing.

---

## Verdict

The Phase 2 exit criterion was *"running the harvest twice produces zero duplicate bronze
objects, and a 429 is retried rather than fatal."*

| Harvest | Snapshot | Discourse | Objects | Note |
| --- | --- | --- | --- | --- |
| 1 | 2 written / 98 | 29 written / 46 | 262 → 300 | first run after the projection fix — one-time rehash |
| 2 | **0** / 100 | 8 written / 67 | 300 → 308 | tail of the transition |
| 3 | **0** / 100 | **0** / 75 | 308 → **308** | **exit criterion met** |
| 4 | **0** / 100 | 75 written / 0 | 301 → 376 | post-level hardening — every key invalidated, as predicted |
| 5 | **0** / 100 | **0** / 75 | 376 → **376** | **stable under the hardened projection** |

Harvests 3 and 5 each wrote nothing and left the object count unmoved. Harvest 4's full
75-object rewrite was the expected consequence of changing `VOLATILE_FIELDS` — the content
hash *is* the key, so editing the projection renames every object once.

The 429 half of the criterion is covered by deterministic unit tests rather than by waiting
to be throttled in the wild.

**78 tests:** 42 unit, 22 integration, 14 live contract tests.

---

## What was built

```
config/protocols.py                     five protocols, space ids verified live
data_pipeline/extraction/http.py        token bucket + retry           151 lines
data_pipeline/extraction/bronze.py      content-addressed writer       175
data_pipeline/extraction/snapshot_client.py   GraphQL, cursor paging   121
data_pipeline/extraction/discourse_client.py  REST + volatile fields    90
data_pipeline/harvest.py                orchestration and CLI          212
```

```bash
make harvest ARGS="--all --proposals 20 --topics 15 --with-posts"
```

### Coverage

Space ids were resolved against the live API rather than guessed — `aave.eth` looks
plausible and returns nothing.

| Protocol | Snapshot space | Forum |
| --- | --- | --- |
| Aave | `aavedao.eth` | governance.aave.com |
| Uniswap | `uniswapgovernance.eth` | gov.uniswap.org |
| Arbitrum | `arbitrumfoundation.eth` | forum.arbitrum.foundation |
| Optimism | `opcollective.eth` | gov.optimism.io |
| ENS | `ens.eth` | discuss.ens.domains |

### Bronze layout

```
snapshot/space=aavedao.eth/<proposal_id>/<content_hash>.json
discourse/space=gov.uniswap.org/<topic_id>/<content_hash>.json
_manifests/<run_id>.json
```

No date appears in a data key. That is the whole idempotency mechanism: an unchanged
document hashes to a key that already exists, so the write is skipped; an edited document
hashes differently and lands *beside* its predecessor. Bronze accumulates version history as
a side effect, which is what Phase 3's SCD2 tables are built from and what makes an
embedding-model change replayable without re-fetching.

Timestamps live only in run manifests, deliberately outside the data prefixes.

---

## The finding that shaped the phase

The first re-harvest wrote **19 Discourse objects despite nothing being edited**. Diffing two
stored versions of the same topic explained it:

| Field | Occurrences across 19 topics |
| --- | --- |
| `suggested_topics` | 904 |
| `views` | 4 |
| `timeline_lookup` | 3 |
| `message_bus_last_id` | 1 |

None of that is governance content. `suggested_topics` is a randomised "related threads"
sidebar; `views` is a hit counter; `message_bus_last_id` is a realtime cursor. Discourse
decorates every response with request-scoped state, so hashing the raw payload made an
unedited thread look edited on every fetch.

**Fix:** hash a *content projection*, store the full response. Identity comes from the
governance content; the body written to S3 is still the complete, unmodified API response.

The duplicate was visible in the data as two objects for topic 1385 with **identical byte
size** and different hashes — later confirmed to be exactly the raw-hash and projected-hash
versions of the same content.

### Second pass: post-level telemetry

Topic-level noise vanished, but a diff restricted to same-projection versions showed it
persisting one level down, inside `post_stream.posts`:

| Post field | Occurrences | What it is |
| --- | --- | --- |
| `reads` | 32 | how many people read the post |
| `readers_count` | 32 | same family |
| `score` | 22 | Discourse's computed post score |
| `posts_count` | 20 | the **author's** forum-wide tally, not the thread's |
| `incoming_link_count` | 6 | inbound links |
| `trust_level` | 1 | author's forum standing |

Harvest 3 passed without this fix because its window was short. Over a daily schedule read
counters move on every active thread, which would manufacture phantom "edits" for Phase 3's
SCD2 build to trip over. `prune` was extended with a `[]` wildcard so paths can traverse
lists (`post_stream.posts[].reads`) and those six fields were excluded.

**Deliberately not excluded**, because they move only on real activity: topic-level
`posts_count`, `last_posted_at`, `like_count`, `word_count`, `participant_count`, and
post-level `reactions`, `actions_summary`, `link_counts`.

Four tests pin both directions — read counters must not create versions; edited bodies and
new replies must.

---

## Snapshot vote churn is real, not noise

Two proposals rewrote during harvest 1. Diffing them showed genuine state change:

```
0x4ec0c13baf55472ecd53…  state=active
    votes          55        -> 56
    scores_total   210039.0  -> 230224.0
```

Both `state=active`. This is correct behaviour and the data is wanted — vote trajectory
feeds the "voting centralization trends" analysis the platform is for.

The consequence for the exit criterion is worth stating precisely: **zero-duplicate
re-harvest holds strictly for closed proposals.** Active ones legitimately version as votes
arrive. That is signal, and the SCD2 design in Phase 3 is built to absorb it.

---

## Deviations from the plan

**Bronze keys are not date-partitioned.** The plan specified
`source/space/ingest_date/`. A date in the key means tomorrow's unchanged re-harvest is a
duplicate by construction, which contradicts the same plan's exit criterion. Keys are
content-addressed instead; the harvest date lives in run manifests.

**`aave.eth` does not exist.** The real space is `aavedao.eth`. An id that silently returns
nothing is indistinguishable from a working pipeline with no new proposals, so a live
contract test now asserts every configured space exists and has a non-zero proposal count.

**Compound was swapped for ENS.** Compound has 36 Snapshot proposals — it governs mostly
on-chain — against ENS's 98 and a busy forum. Better data for the same effort.

**A `live` test marker was added.** Contract tests against Snapshot and Discourse are
excluded from `make verify`; a red build caused by someone else's maintenance window teaches
nothing. Run them deliberately with `make verify-live`. Their job is catching upstream API
drift, which is the likeliest way ingestion breaks silently.

---

## What the tests assert

**Rate limiting and retry (unit, no network).** Token bucket burst, throttle and refill
behaviour with an injected clock, so no test actually sleeps. Retry covers: 429 honouring
`Retry-After` verbatim, exponential backoff with growth when the header is absent, 5xx,
transport errors, a bounded attempt limit that raises rather than looping forever, and — the
inverse — a 404 that must **not** be retried, since retrying a bad request only burns rate
budget a real request needs.

One test-double bug was caught here: `FakeResponse` initially lacked `raise_for_status`,
which would have let a broken error path pass. Fixed in the double, not by weakening the
client.

**Content addressing (unit).** Hash stability across dict key ordering, nested structures and
unicode; hash sensitivity to real changes; keys free of date components; path-hostile entity
ids (Discourse slugs contain slashes) escaped so they cannot invent key prefixes.

**Bronze writes (integration, real MinIO).** Idempotency asserted by counting objects in the
bucket, not by trusting the writer's own return value. Also: an edit lands beside the
original rather than overwriting; five repeated harvests stay at one object; object metadata
records content hash, source and observation time.

**Live contract tests.** Every configured space exists and is populated; proposals carry
`state` (not `status`) and the `discussion` field that joins Snapshot to Discourse;
pagination advances rather than repeating; all five forums serve topics; topic detail
includes post bodies.

---

## Known gaps

**Changing the projection rewrites everything once.** The content hash defines the key, so
editing `VOLATILE_FIELDS` invalidates every existing object and the next harvest re-writes
each document under its new name. This happened twice during Phase 2 and accounts for the
writes in harvest 1. It is acceptable — bronze is append-only and the change is rare — but it
should be a deliberate decision, not a casual edit. A scheme version in the key was
considered and rejected as premature.

**Only the recent window is harvested.** `--proposals 20 --topics 15` per protocol, not full
history. Aave alone has ~970 proposals. A full backfill is a rate-budget exercise, not a code
change, and is better done once Phase 3 can consume it.

**Discourse volume dominates.** ~60 KB per topic against ~6 KB per proposal. Relevant to
Phase 4 chunking and embedding cost, where the forum corpus will be the bulk of the spend.

**Rate limits are assumed, not measured.** 60/min for Snapshot and 30/min per Discourse host
are conservative guesses. No 429 was ever observed, so the retry path has never fired against
a real server — only against the fake transport.

**The acceptance harness used to destroy harvested data.** `scripts/verify.sh` opens with
`docker compose down -v`, which wiped all 376 objects when it was run to confirm the suite
still passed before committing. Fixed after Phase 2: the harness now runs under its own
Compose project with separate volumes and ports, verified by harvesting into the dev stack,
running the full harness, and confirming the object count was unchanged. Three tests pin the
invariant. The dev stack is still destroyed by `make nuke`, which is what that name is for.

**Full topic bodies require `--with-posts`.** Without it only listing metadata is stored,
which has no post text in it. Easy to forget; Phase 3 should fail loudly on a topic object
with no `post_stream`.

---

## Data harvested

Final census, excluding the `probe-*` fixtures the integration suite writes.

| Prefix | Objects | Distinct documents | Size |
| --- | --- | --- | --- |
| discourse/governance.aave.com | 65 | 16 | 3.04 MB |
| discourse/discuss.ens.domains | 60 | 15 | 3.02 MB |
| discourse/forum.arbitrum.foundation | 52 | 15 | 3.61 MB |
| discourse/gov.uniswap.org | 50 | 15 | 4.12 MB |
| discourse/gov.optimism.io | 47 | 15 | 2.71 MB |
| snapshot/aavedao.eth | 22 | 20 | 0.18 MB |
| snapshot/arbitrumfoundation.eth | 20 | 20 | 0.29 MB |
| snapshot/ens.eth | 20 | 20 | 0.20 MB |
| snapshot/uniswapgovernance.eth | 20 | 20 | 0.13 MB |
| snapshot/opcollective.eth | 20 | 20 | 0.03 MB |
| **Total** | **376** | **176** | **17.34 MB** |

The gap between the two count columns is the version history. Snapshot sits at 1.03
objects per proposal — only the two actively-voted proposals ever gained a version.
Discourse sits at ~3.4, which is almost entirely the two projection changes rewriting every
topic; steady-state churn after harvest 4 was zero.

Discourse averages ~60 KB per topic against ~6 KB per proposal. That ratio, not the object
count, is what matters for Phase 4 embedding cost.

---

## Next

Phase 3 — the Spark job that reads bronze, computes `content_hash`, and builds the SCD2
`proposal_versions` and `forum_posts` Iceberg tables. Bronze already carries the version
history it needs, so the job is a fold over content-addressed objects rather than a diff
against a live API.

No API keys were needed for Phase 2 and none are needed for Phase 3. The first key required
is OpenAI, at Phase 4.
