# Phase 4 Exit Demo — Building `gov search`, and What Running It Found

**Status:** Phase 4 exit criteria met · **Date:** 17 Aug 2026

The implementation plan specifies a demo for Phase 4:

> **Demo:** `gov search "delegate voting power concentration"` returns ranked chunks with
> protocol, proposal and date.

Running it was supposed to be a formality. It found three things instead, and two of them
were more valuable than the demo.

| # | Finding | Cost to fix |
| --- | --- | --- |
| 1 | The command did not exist — `search()` had no entry point | new CLI |
| 2 | The date the demo asks for was not in the database, and the obvious column is wrong in a way that looks right | $0 — metadata backfill, no re-embedding |
| 3 | The demo query returns nothing, and never worked under any chunk scheme | plan updated |
| ⭑ | Prompted by 3: the relevance cutoffs were fitted on v1 and never refitted after the v2 re-embed | $0 — measured, still valid |

---

## 1. The command did not exist

`search()` was a Python function. The plan had been describing a CLI for four phases and
nothing had ever called it from a shell.

```
ai_agent/cli.py          `gov`, installed via [project.scripts]
```

```bash
gov search "oracle deprecation for long-tail assets"
gov search "treasury diversification" --protocol arbitrum --source proposal -k 10
gov search "delegate incentives" --no-threshold      # show the raw ranking
gov search "..." --json | jq                          # machine-readable
make search Q="ArbOS 60 Elara upgrade"
```

It is deliberately thin: it formats what `search()` returns and does nothing else — no
re-ranking, no filtering of its own, no synthesis. If the demo output looks wrong, the
retrieval layer *is* wrong. Synthesis arrives in Phase 5 with the router; this prints
evidence, not answers.

One design decision worth stating. When retrieval returns nothing, the CLI says so and exits
**0**:

```
"delegate voting power concentration"  —  no sufficiently relevant chunks
  The corpus has nothing close enough to answer this. Re-run with
  --no-threshold to see the nearest matches and their distances.
```

An empty result is the two-signal threshold working, not a failure. Exiting non-zero, or
printing five confident-looking irrelevant chunks, would both misrepresent what happened.

---

## 2. The date the demo asks for was not in the database

`document_embeddings` carried `valid_from`, and rendering that as "date" would have been a
one-word change. It is the wrong column, and wrong in the most dangerous way available:

```sql
SELECT min(valid_from)::date, max(valid_from)::date FROM document_embeddings;
--  2026-08-14 | 2026-08-14
```

`valid_from` is the SCD2 validity window's start — **when the pipeline first observed the
row**, not when the governance event happened. This corpus was harvested in a single pass,
so every one of the 3,310 chunks shares the same value. A listing built on it would have
printed `2026-08-14` beside every result: correctly typed, plausibly formatted, and
completely uninformative.

The scale of what was being flattened is worth seeing. The governance dates span **247
distinct days across six years** (2020-09-21 to 2026-08-14); `valid_from` spans **one**.

The real dates were in silver the whole time and had simply never been carried across —
`proposal_created` for proposals, `post_created_at` for forum posts.

### The fix, and why it was free

Two columns added as **citation metadata**, explicitly not part of ranking:

```sql
title                VARCHAR(512)
document_date        TIMESTAMPTZ   -- governance date, NOT valid_from
```

`scripts/backfill_citation_metadata.py` filled all 3,310 existing rows from silver:

```
silver: 453 rows, 442 distinct content hashes
pgvector: 3310 chunk(s) missing citation metadata
done: 3310 filled, 0 still null
```

**No re-embedding.** The vectors are untouched, so this cost nothing — the whole point of
separating metadata from the embedded text. The loader now populates both fields going
forward, and `make embed --dry-run` still reports `nothing to embed — everything is
current`, confirming the schema change did not disturb the idempotency check.

The backfill keys on `source_content_hash` rather than `document_id`. That is deliberate:
`source_content_hash` is silver's identity for the *text a chunk was built from*, so a chunk
embedded from a superseded version gets that version's title and date rather than today's.
For the same reason the backfill query does not filter on `is_current` — doing so would
leave every superseded row permanently null.

### A second bug found while applying this

```sql
CREATE INDEX IF NOT EXISTS document_embeddings_lookup_idx
    ON document_embeddings (protocol_name, source, document_id, embedding_model, chunk_scheme);
```

The live index was still missing `chunk_scheme`. `IF NOT EXISTS` matches on the index
**name**, not its definition — an index of that name already existed, so the updated
definition was silently skipped. The schema file and the database had disagreed since the v2
work landed, and nothing reported it. Rebuilt with an explicit `DROP` / `CREATE`.

Worth generalising: `CREATE ... IF NOT EXISTS` is not a migration. It is a no-op guard, and
it will happily leave a stale definition in place forever.

---

## 3. The demo query returns nothing, and never worked

```
$ gov search "delegate voting power concentration"
"delegate voting power concentration"  —  no sufficiently relevant chunks
```

Both cutoffs reject it:

```
best distance   0.551   (ceiling 0.48)   FAIL
gap (mean-best) 0.024   (floor  0.025)   FAIL
profile over 10: 0.551 0.561 0.566 0.574 0.575 0.577 0.579 0.589 0.591 0.593
```

### It is not a v2 regression

The v1 vectors are still in the table, so this is checkable rather than arguable:

| scheme | best | gap | verdict |
| --- | --- | --- | --- |
| v1 | 0.530 | 0.038 | rejected (distance) |
| v2 | 0.551 | 0.024 | rejected (distance **and** gap) |

v1 rejected it too. The query was written into the plan *before the corpus existed* and was
never run until the exit demo — which is the actual lesson here.

### Rephrasing does not rescue it

The obvious hypothesis is that a bare keyword phrase embeds poorly compared to a natural
question. Measured, that is not the explanation:

| Phrasing | best | gap | |
| --- | --- | --- | --- |
| `delegate voting power concentration` | 0.551 | 0.024 | reject |
| *Is voting power concentrated among a few delegates?* | 0.551 | 0.030 | reject |
| *Which proposals address concentration of delegate voting power?* | 0.539 | 0.012 | reject |
| *How do DAOs try to reduce reliance on a small number of large delegates?* | 0.442 | 0.035 | **accept** |

Three of four natural phrasings still reject. One works, and it works because it names a
*mechanism* the corpus actually discusses (reliance on large delegates) rather than an
*abstraction* it does not (concentration as a measured property).

### The diagnosis is the corpus

The decisive number: this query's best hit at **0.551 is further away than every
deliberately-unanswerable negative in the eval set** (0.432–0.529). By the corpus's own
geometry it looks *less* answerable than the questions written specifically to be
unanswerable.

Looking at what it does retrieve explains why:

```
0.551  ENS       [6.31] [Temp Check] Delegation Incentives Program
0.561  Uniswap   Treasury Delegation Round 2 Elections
0.566  Arbitrum  Welcome to Discourse
0.596  Arbitrum  [Constitutional] DVP Quorum for ArbitrumDAO
```

There is material *adjacent* to the topic — delegation incentives, treasury delegation,
quorum based on registered vs voteable supply — but **no document about concentration of
delegate voting power**. The corpus can gesture at the question; it cannot answer it.

So the threshold is behaving correctly. For a governance risk tool, declining to answer is
the right output when the evidence is a collection of loosely related documents. The failure
mode this avoids is the one that matters: four plausible-looking citations assembled into a
confident answer to a question nobody in the corpus actually addressed.

### What was changed

The plan's demo line now uses a query the corpus supports, with the original retained as a
worked example of the threshold refusing to bluff. The demo command itself was already
correct — it was the query that was aspirational.

```
$ gov search "How do DAOs try to reduce reliance on a small number of large delegates?" -k 3

1. Uniswap  forum  2024-08-31  distance 0.442
   Uniswap-Arbitrum Delegate Program (UADP) Communication Thread
2. Arbitrum  proposal  2026-03-26  distance 0.471
   Updating the Code of Conduct & DAO Procedures to Become Living Documents
3. ENS  proposal  2026-02-04  distance 0.473
   [6.31] [Temp Check] Delegation Incentives Program
   section: Why this matters
   ... More active voting power raises capture cost and reduces capture risks. A strong
   base of active delegates reduces reliance on emergency mechanisms ...
```

---

## ⭑ The check that mattered more

Investigating finding 3 surfaced a hazard nobody had flagged: **`DEFAULT_MAX_DISTANCE` and
`DEFAULT_MIN_GAP` were fitted against v1 embeddings and were never refitted after the v2
re-embed.** Constants tuned on one vector space, applied to another, with the eval set
reporting healthy numbers throughout — because recall is scored with the cutoffs *off*, and
only the five negatives exercise them.

That is precisely the shape of a problem that hides. It is also directly measurable, because
v1's vectors were retained specifically so a decision like this could be revisited.

Best distance and gap, per question kind, across all 24 eval questions:

| Question kind | best v1 | best v2 | shift | gap v1 | gap v2 |
| --- | --- | --- | --- | --- | --- |
| lookup | 0.319 | 0.286 | **−0.032** | 0.077 | 0.077 |
| thematic | 0.374 | 0.346 | **−0.028** | 0.042 | 0.046 |
| cross_source | 0.307 | 0.289 | **−0.018** | 0.050 | 0.063 |
| negative | 0.472 | 0.472 | **+0.000** | 0.026 | 0.022 |

```
v1: positives 0.215..0.462   negatives 0.431..0.521   rejects 1 positive
v2: positives 0.216..0.458   negatives 0.432..0.529   rejects 1 positive
```

**v2 pulled every positive category closer to the corpus while leaving the negatives exactly
where they were.** The separation between answerable and unanswerable widened rather than
drifting, and both schemes reject the same single positive
(`theme-delegate-incentives`).

The fitted constants remain valid under v2 — now as a recorded measurement rather than an
assumption.

This is also a better argument for keeping chunk scheme v2 than its headline
recall number was. The +0.03 micro recall was a mixed result with a real regression in
`lookup` (see [phase-4-retrieval.md](phase-4-retrieval.md)); *this* says the protocol label
moved answerable questions toward the corpus without moving unanswerable ones, which is the
property you actually want from a retrieval change.

---

## Reproducing all of it

```bash
# the demo
make search Q="How do DAOs try to reduce reliance on a small number of large delegates?"
gov search "delegate voting power concentration"              # returns nothing, correctly
gov search "delegate voting power concentration" --no-threshold -k 8

# the date trap
docker compose exec postgres psql -U engineer -d gov_vectors -c \
  "SELECT count(DISTINCT document_date::date) AS governance_dates,
          count(DISTINCT valid_from::date)    AS harvest_dates,
          min(document_date)::date AS earliest, max(document_date)::date AS latest
   FROM document_embeddings;"
#  governance_dates | harvest_dates |  earliest  |   latest
#               247 |             1 | 2020-09-21 | 2026-08-14

# metadata backfill (idempotent, no API calls)
make backfill-metadata ARGS=--dry-run

# the loader is undisturbed
make embed ARGS=--dry-run     # "nothing to embed — everything is current"
```

---

## Tests

**20 new.** The CLI ones need neither a database nor a credential — it is a formatting layer,
so it is tested against constructed `SearchResult`s.

| File | Count | Needs |
| --- | --- | --- |
| `tests/test_phase4_cli.py` | 16 | nothing |
| `tests/integration/test_phase4_retrieval.py` (metadata) | 4 | Postgres |

The ones carrying real weight:

- **`test_document_date_is_not_the_harvest_date`** — asserts governance dates span >50
  distinct days while `valid_from` spans ≤2. This is the finding-2 trap pinned so it cannot
  silently return.
- **`test_the_date_shown_is_the_governance_date_not_the_harvest_date`** — the same invariant
  at the render layer, because reading the wrong field produces output that looks fine.
- **`test_document_dates_are_not_in_the_future`** — a unix-seconds field decoded as
  milliseconds lands in the year 57000 and nothing upstream complains.
- **`test_titles_match_the_prefix_embedded_in_v2_chunk_text`** — the stored title and the
  title inside the embedded text come from one silver column; if they drift, a citation
  names one document while the evidence quotes another.
- **`test_empty_results_explain_themselves_and_exit_zero`** — "nothing found" must be
  distinguishable from "the tool broke".
- **`test_no_threshold_disables_both_cutoffs`** — disabling only `max_distance` leaves
  `min_gap` filtering, which is exactly the bug that made an integration test's "unfiltered"
  control arm return nothing.

Suite totals: **92 unit, 70 integration** (21 of those `live`), all passing.

---

## What this says about method

Three of the four findings share a shape: **something that had never been executed end to
end, agreeing with itself right up until it was.**

- A demo query written before the corpus existed, unrun for four phases.
- A date column that was never rendered, so nobody noticed it was the wrong one.
- `CREATE INDEX IF NOT EXISTS` that reported success while skipping its own definition.
- Thresholds fitted on v1, silently inherited by v2, with the eval set structurally unable
  to notice because recall is scored with the cutoffs off.

None of these produced an error. Each was found by running the thing for real and comparing
the result against a written-down expectation. That is the same pattern as the earlier
`min_gap` window bug — the sweep predicted 5/5 negatives blocked, measurement said 3/5, and
the disagreement *was* the bug.

The corollary for Phase 5: write the router's evaluation before the router.

---

## What's next

Phase 4's exit criteria are met — a baseline recall number is committed, and the demo runs
against the real corpus.

Phase 5 is the LangGraph intent router, which consumes `search()` as it stands. Two things
carry forward from here:

1. **Protocol filtering belongs to the router.** Chunk scheme v2 tested whether text alone
   could fix protocol bleed; measured, it cannot (`aave-v4-deployment-targets` went 0.67 →
   0.33). The filter works — `protocol=aave, source=proposal` takes that question from 0/3
   to 2/3 — but applying it requires parsing the question, and a wrong guess excludes the
   answer entirely. That is a decision that needs intent.
2. **The eval set never exercises the cutoffs on positives.** Recall is scored with them off
   and only the negatives turn them on, which is how v1-fitted constants survived a re-embed
   unexamined. Worth adding an end-to-end mode that scores positives *with* the cutoffs
   engaged, so a false rejection costs a number instead of going unnoticed.
