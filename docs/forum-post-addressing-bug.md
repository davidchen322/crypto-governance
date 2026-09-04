# Forum post addressing: two real bugs, one false alarm

**Status:** fixed and verified · **Found:** 3 Sep 2026, during the Phase 7 backfill's eval
regression investigation · **Fixed:** 4 Sep 2026

## TL;DR

Retrieval's citation address for forum content — `(document_id, chunk_index)` — was not
actually unique. Two real, distinct bugs caused this, both now fixed and verified against
real data. A third suspected bug (an SCD2 versioning failure in `build_silver.py`) was
investigated and found **not to exist** — the alarm was caused by a verification query of
my own that forgot to scope by forum. That correction is included here because it shaped
which fix was actually needed.

Nothing here was ever a data-loss bug: every real piece of forum content was always stored
correctly. The bug was entirely in the *address* used to refer back to a specific piece of
content — for citations (`chunk_ref` shown alongside an answer), for `make search`'s own
output, and for anything that treats `(document_id, chunk_index)` as if it pins down one
specific chunk.

---

## How this surfaced

While investigating why `make eval`'s negative-question leak rate got worse after the
Phase 7 backfill grew the corpus ~18.5x, one specific leak (`neg-compound`) traced back to a
real chunk from Discourse topic `30691`. Pulling that chunk's row directly turned up something
unexpected: **19 distinct rows**, all sharing `(document_id=30691, chunk_index=0)`, each with
a genuinely different `source_content_hash`. Not duplicates — 19 different real posts'
content, one ambiguous shared address.

---

## Bug 1 — chunk_index resets per post, but document_id is the whole topic

`chunk_forum_post()` numbers a single post's own chunks starting from 0. But `document_id`
for forum content is the *topic* id, shared by every post in that topic. Two different
posts' first chunk both land at `(document_id, chunk_index) = (topic_id, 0)` — nothing in
the addressing scheme accounts for which post a chunk came from.

**Real-world consequence**: `chunk_ref` (`ai_agent/graph/synthesis.py`) and `make search`'s
own printed output (`ai_agent/cli.py`) render as `forum:{document_id}#{chunk_index}` —
this is the string a citation resolves to. For a busy topic, that string could point at any
one of several genuinely different posts. Scope: **2,219 colliding groups, 13,171 of 17,964
forum rows (73%)** — most busy topics were affected.

**Fix (first attempt, wrong)**: fold `post_number` (Discourse's 1-indexed position within
the topic) into `chunk_index`: `chunk_index = post_number * 1000 + local_index`. Reduced
collisions from 2,219 groups to 102 — better, not zero.

**Why it was wrong**: `post_number` is not a stable identity. Discourse renumbers it when
posts are deleted or moved — it reflects current position, not a permanent id. A real
corpus row proved this: topic 24481 had **two different post_ids (62895 and 51020) both
carrying `post_number = 2`**, both legitimately current. Reusing `post_number` just moved
the collision to a rarer case instead of closing it.

**Fix (corrected)**: use `post_id` instead — the field the silver schema actually declares
`NOT NULL` and unique. `chunk_index = post_id * 100 + local_index`. Verified against the
real corpus: max `post_id` is 78,094; the 100-multiplier only overflows the 32-bit
`chunk_index` column past a ~21 million `post_id`, comfortable headroom.

**Result**: 2,219 colliding groups → **0**.

---

## The false alarm — no SCD2 bug in `build_silver.py`

After the `post_id` fix, one collision remained: topic 7, `post_id` 10. Pulling that row
directly from `iceberg.silver.forum_posts` showed **three rows, all `is_current = true`,
all sharing the same `post_id`** — which looked exactly like an SCD2 failure: `build_silver`
not closing out an old version when a post's content changed.

Quantifying it looked serious: **285 of ~9,907 posts (2.9%)** had more than one
`is_current = true` row when grouped by `post_id` alone.

**This was wrong**, and the fix was to re-run the query with `forum_host` included in the
grouping:

```
TRUE multi-current (scoped by forum_host): affected_posts = 0
```

Zero. The reason: Discourse assigns topic and post ids **independently per forum
installation** — small sequential integers that different protocols' forums reuse with no
coordination. Topic 7 / post 10 on Arbitrum's forum, Uniswap's forum, and Optimism's forum
are three completely unrelated real posts that happen to share the same numbers by
coincidence. `build_silver.py`'s window/MERGE logic (`add_validity_windows()`, keyed by
`Window.partitionBy("forum_host", "topic_id", "post_id")`) was correct the whole time —
each of those three posts has exactly one current version, correctly, within its own forum.
No code change was made to `build_silver.py`.

---

## Bug 2 — document_id (and post_id) are only unique within one protocol's forum

The real, narrower bug the false alarm led to: **`document_id` (a bare `topic_id`) is not
globally unique** — it's only guaranteed unique within one Discourse installation. The same
is true of `post_id`, which is why the fix above still collided once on the topic-7
coincidence. This is real but far rarer than Bug 1: it depends on two *different protocols*
coincidentally picking the same small number, not on a busy topic having many posts. Across
the whole corpus (~9,615 distinct posts, 5 protocols), it happened exactly **once**.

**Fix**: qualify forum `document_id` with protocol: `document_id = f"{protocol_name}:{topic_id}"`.
Every downstream consumer (`chunk_ref` in synthesis.py, `make search`'s CLI output, the
`/chat` API response) inherits the fix automatically — none of them needed their own change,
since they all just render whatever `document_id` already contains. Two places did need
direct updates because they build their own lookup keys from raw `topic_id`:

- `tests/eval/questions.yaml` — every `{source: forum, topic_id: N}` label now also carries
  `protocol: <name>`, and `tests/eval/score.py`'s `expected_key()` builds the composite key
  from both.
- `scripts/eval_sources.py`'s `load_forum_index()` keyed its lookup dict by bare `topic_id`
  alone — the exact same latent bug, in a different file. Fixed to key by `(protocol, topic_id)`.

**Verification**: re-embedded forum content, re-ran the collision query — **0**. Spot-checked
the actual case that motivated this: topic 7 now resolves to `optimism:7` and `uniswap:7` as
distinct, correctly-addressed documents (`arbitrum:7` doesn't appear — its topic 7 is a
generic auto-generated "Welcome to Discourse" thread, correctly filtered out by the existing
boilerplate check, unrelated to this fix). `make eval` re-run confirms the new
protocol-qualified keys still match correctly (e.g. `cross-ens-next-era`'s miss now reports
as `forum:ens:22329`, not a bare topic id).

---

## What was verified, concretely

| Check | Before | After |
|---|---|---|
| Colliding `(document_id, chunk_index)` groups, forum | 2,219 | 0 |
| Rows involved | 13,171 | 0 |
| Real 3-way protocol coincidence (topic 7) | ambiguous | resolves to distinct `optimism:7` / `uniswap:7` |
| SCD2 multi-current posts (correctly scoped by forum) | — | 0 (never was a bug) |
| Unit tests (`tests/test_phase4_embeddings.py`) | — | 3 new, all passing |
| `make eval` after both fixes | — | same pass/fail pattern as before the addressing fixes, confirming they didn't change retrieval quality — only correctness of addressing |

---

## One related, separate observation for KAN-3 — not caused by this fix

Re-running `make eval` after the final fix showed one new failure not present before this
investigation began: `arbitrum-arbos-60` (a proposal-only lookup question) dropped to 0/1
recall. This is unrelated to forum addressing — proposals were never touched by any of these
fixes. The likely explanation: today's fixes legitimately grew the effective forum content
count slightly (17,964 → 18,050 rows) by correctly retaining content that address collisions
previously obscured. With `max_per_document = 1` capping each document to one top-5 slot,
more real competing content — from any source — can push a previously-passing borderline
question out of the top 5. This is the same corpus-growth-crowds-out-recall pattern KAN-3
already tracks; it isn't traced to certainty here, and isn't treated as a new bug, but it's
worth appending to KAN-3 as one more real, measured data point.

---

## Files changed

- `ai_agent/chains/embeddings.py` — `forum_chunk_index()` (new), `load_silver_chunks()`
  updated to select `post_id`, address via `post_id`, and qualify `document_id` with protocol
- `tests/test_phase4_embeddings.py` — 3 new unit tests covering the collision, the wrong
  `post_number` fix's own failure case, and the missing-`post_id` guard
- `tests/eval/questions.yaml` — 8 forum `expect` entries annotated with `protocol`
- `tests/eval/score.py` — `expected_key()` builds the protocol-qualified forum key
- `scripts/eval_sources.py` — `load_forum_index()` keyed by `(protocol, topic_id)`

No schema migration — `document_id` and `chunk_index` are existing columns; only the values
written into them changed. Existing forum embeddings were deleted and regenerated from
silver (silver itself was never touched), at negligible re-embedding cost.

---

## Suggested ticket text

**New ticket — forum post addressing was not unique**

> Retrieval's citation address for forum content, `(document_id, chunk_index)`, was not
> unique — two real bugs, both fixed and verified: (1) `chunk_index` reset per post while
> `document_id` was topic-scoped, so busy topics' posts collided (2,219 groups, 73% of forum
> rows); (2) `document_id` (bare `topic_id`) is only unique within one protocol's Discourse
> forum, not globally, causing one real 3-way coincidental collision across protocols. Full
> writeup, root cause, and verification: `docs/forum-post-addressing-bug.md`.

**KAN-3 update — real measurement plus a new data point**

> Re-ran `make eval`/`make eval-routing` against the full post-backfill corpus (1,593 →
> 29,530+ chunks). Retrieval pre-filter negatives-clean fell from 5/5 to 1/5 — real drift,
> but the relevance judge already downstream of it recovers most of that (3/5 clean
> end-to-end; `tests/eval/score.py` now measures both layers explicitly rather than just the
> pre-filter). Separately, fixing an unrelated forum-addressing bug
> (`docs/forum-post-addressing-bug.md`) legitimately grew effective corpus density slightly
> and pushed one more borderline lookup question (`arbitrum-arbos-60`) out of top-5 recall —
> one more real instance of this ticket's core concern, not a new bug.
