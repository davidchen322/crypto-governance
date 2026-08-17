# Phase 4 — Embeddings, Retrieval and the Baseline

**Status:** Baseline measured, two of three findings fixed · **Date:** 16 Aug 2026

## Current numbers

| Metric | First baseline | After diversity + gap threshold |
| --- | --- | --- |
| micro recall@5 | 0.67 | **0.78** |
| macro recall@5 | 0.82 | **0.88** |
| lookup | 0.88 | **0.96** |
| thematic | 0.50 | **0.60** |
| cross_source | 1.00 | 1.00 |
| negatives clean | 0/5 | **5/5** |

Both fixes cost nothing — no re-embedding, no extra tokens handed to the analyst. Details
in *Fixes applied* below. The original baseline and its analysis follow, kept because the
reasoning is what makes the numbers meaningful.

---


The corpus is embedded, `search()` works, and the eval set has produced a number. That
number is not good, which is the point — it is now possible to tell improvement from
change.

---

## Baseline

`make eval` · k=5 · 1,655 chunks · `text-embedding-3-large` @ 1536 dimensions

| Question kind | Recall@5 | Questions |
| --- | --- | --- |
| cross_source | **1.00** | 6 |
| lookup | **0.88** | 8 |
| thematic | **0.50** | 5 |
| **micro** (documents found / expected) | **0.67** | |
| **macro** (mean per-question) | **0.82** | |

| Negatives | Result |
| --- | --- |
| Clean at threshold 0.75 | **0 / 5** |

Recorded in `tests/eval/baseline.json`. Every later change gets compared against this file.

**Read the two halves separately.** Recall is scored with the distance threshold *off*,
because it measures the ranking. Negatives are scored with it *on*, because they measure
the cutoff. Blending them into one number would let a loose threshold hide inside good
recall — which, as it turns out, is exactly what is happening here.

---

## What was built

```
ai_agent/chains/chunking.py       structure-aware splitting (documented separately)
ai_agent/chains/trino_client.py   minimal Trino REST reader
ai_agent/chains/embeddings.py     idempotent loader
ai_agent/chains/retrieval.py      search() over pgvector
tests/eval/score.py               recall@k scorer
```

```bash
make embed ARGS=--dry-run   # cost and count, no API calls
make embed                  # only what is missing
make eval ARGS="--verbose --save"
```

### The loader never pays twice

Idempotency was the design constraint, since this is the first component that costs money.
The loader chunks everything, asks Postgres which `(source_content_hash, chunk_index,
embedding_model)` triples already exist, and embeds only the remainder — *before* calling
the API, not via `ON CONFLICT` afterwards. Paying for a vector and then discarding it is
still paying for it.

Measured:

```
first run   1,655 rows written, 419,826 tokens billed   (~$0.055)
second run  0 API calls — "nothing to embed, everything is current"
```

This is where Phase 3's two-hash split earns itself. `content_hash` covers title and body
only, so a proposal whose vote tally moved is a new silver *row* with the same text, and
the loader correctly declines to re-embed it. Had versioning and text identity shared one
hash, every vote landing on an active proposal would have re-billed its embeddings.

1,666 chunks produced 1,655 rows. The 11-row gap is boilerplate repeated verbatim across
forums, collapsing onto the UNIQUE constraint — free deduplication, and the same gap
visible in silver as 328 posts sharing 317 distinct content hashes.

---

## Findings

### 1. Chunk domination — the biggest single problem

Top-5 chunks contain, on average, **2.0 distinct documents**. Three of the five slots are
redundant chunks from documents already represented.

| k | Mean distinct documents | micro recall |
| --- | --- | --- |
| 5 | 2.0 | 0.67 |
| 10 | 3.0 | 0.78 |
| 20 | 5.1 | 0.83 |

This is why thematic questions score 0.50. A question expecting four documents *cannot*
succeed at k=5 if two documents occupy all five slots. It is not a ranking failure — the
right documents are often present just below the cut — it is a diversity failure.

It was predicted, in the eval set's own notes before any of this ran:

> The interesting failure is a retriever that returns four near-identical chunks from ONE
> of them rather than one chunk from each — good recall, useless answer.

The fix is not a bigger k. Raising k to 20 buys 0.16 recall and hands the analyst four
times the tokens, most of them duplicative. The real fix is **diversity-aware retrieval**:
retrieve k×3 chunks, group by document, keep the best chunk or two per document, return the
top k documents. That is a Phase 4 change, not a Phase 5 one.

### 2. The distance threshold cannot separate answerable from unanswerable

Every one of the five negative questions leaked all five chunks at threshold 0.75. Tightening
it does not cleanly fix this:

```
positive best-distance range:  0.215 … 0.462
negative best-distance range:  0.431 … 0.521
```

**They overlap.** No single global cutoff separates them. A threshold at 0.43 would catch
four of five negatives and wrongly reject two real questions.

That is a genuine, measured limitation, and worth stating plainly rather than tuning until
the number looks nice. Options, roughly in order of effort:

- **Accept the overlap** and set the threshold at ~0.42, taking two false rejections to
  block four of five false accepts.
- **Relative threshold** — reject when the top result is not meaningfully closer than the
  tail, which detects the flat distance profile of an unanswerable question better than an
  absolute cut.
- **Let the analyst judge.** Pass chunks with distances and let the synthesis step decline.
  The blueprint's prompt already has the escape hatch: `DATA GAP IDENTIFIED`.
- **A reranker** — a cross-encoder scoring (query, chunk) pairs directly. Most effective,
  most expensive.

None of these can be chosen sensibly without the eval set, which is the argument for having
built it first.

### 3. Protocol bleed on shared vocabulary

`aave-v4-deployment-targets` scored 0/3. Its top hit was a **Uniswap** forum thread, for an
**Aave** question:

```
0.344  forum  26150   "[RFC] Four for V4 ..."          <- Uniswap
0.345  forum  26150   "[RFC] Four for V4 ..."          <- Uniswap
0.366  forum  24293   "[ARFC] Aave V4 Activation ..."
0.384  forum  24293   "[ARFC] Aave V4 Activation ..."
0.387  forum  24293   "[ARFC] Aave V4 Activation ..."
```

"V4" is shared vocabulary. The embedding has no notion that Aave's V4 and Uniswap's V4 are
unrelated, and the protocol name lives in a column the vector cannot see.

Two of the three expected proposals were also outranked by forum discussion of the same
topic — a general pattern worth watching, since forum text is chattier and more repetitive
than proposal text and so produces more chunks per unit of substance.

Fixes: pass `protocol=` when the question names one (a job for the Phase 5 router, which
already has to parse intent), or put the protocol name into the chunk text alongside the
title.

### 4. Cross-source retrieval is already solid

6/6, every one perfect. Questions requiring both a proposal and its forum thread work well,
including the two clean pairs (`Activate v4 Protocol Fees`, `Four for V4`). Whatever else
needs work, the thing the platform exists to do — connect a vote to the debate around it —
retrieves correctly.

---

## Bugs found while building

**The loader could not read `.env`.** `check_openai.py` had its own dotenv loader; the
embedding loader had none, so it reported `OPENAI_API_KEY is not set` on a machine where
the credential check had just passed. The loader now lives in `config/settings.py` and both
entry points call it. Duplicating it would have left the same trap for the next script.

**A test asserted an impossible SQL expression.** The vector-normalisation check was written
as `embedding <#> embedding * -1`, which is a type error — `<#>` returns negative inner
product, so the L2 norm is `sqrt(-(v <#> v))`. Caught immediately by running it; noted in the
test because the operator's sign is easy to get backwards.

---

## Tests

**27 new, split by cost so the free ones run anywhere.**

| File | Marker | Needs |
| --- | --- | --- |
| `test_phase4_chunking.py` (16) | none | nothing |
| `test_phase4_embeddings.py` (8) | none | nothing |
| `test_phase4_retrieval.py` (7) | `integration` | Postgres |
| `test_phase4_retrieval.py` (6) | `integration, live` | Postgres + API key |

The ones worth knowing about:

- **`test_vectors_are_paired_by_index_not_arrival_order`** — the API returns an `index`
  per embedding and does not promise list order. Trusting order attaches every vector to
  the wrong chunk, and retrieval then returns confident nonsense with no error anywhere.
  The fake HTTP client returns results deliberately reversed.
- **`test_stored_vectors_are_unit_normalised`** — cosine distance assumes it; an
  un-normalised batch ranks plausibly but wrongly.
- **`test_hnsw_index_exists_and_is_used`** — asserts on the query plan, not the index's
  existence. An index that exists but is not chosen is the same as no index.
- **`test_every_chunk_appears_exactly_once`** — an off-by-one in the batch loop silently
  drops the last partial batch, so the tail of the corpus never gets embedded and never
  gets reported missing.
- **`test_point_in_time_search_excludes_unobserved_rows`** — proves `as_of` actually
  filters, so a historical question cannot silently retrieve today's text.

---

## What's next

In priority order, all still Phase 4:

1. **Diversity-aware retrieval.** The single highest-value change. Group by document before
   truncating to k. Expected to move thematic recall substantially without raising token
   cost.
2. **Re-measure, compare to `baseline.json`.** Every change from here gets a before/after.
3. **Threshold strategy.** Pick from the four options above, measured rather than guessed.
4. **Protocol filtering.** Cheap to try: put the protocol name in the chunk text and
   re-embed (~$0.055) to see whether it fixes the V4 bleed.

Then Phase 5 — the LangGraph router, which consumes `search()` as it now stands.

Two things deliberately *not* being done yet: chunk-size tuning (there is no evidence chunk
size is the problem, and changing it re-embeds everything), and raising k as a fix for
diversity (it treats the symptom and costs the analyst four times the context).


---

## Fixes applied

### Diversity-aware retrieval

`search()` now over-fetches `k x 4` chunks and keeps at most one per document
(`max_per_document=1`), so k slots hold k distinct documents rather than 2.0 on average.

micro 0.67 -> 0.78, lookup 0.88 -> 0.96, thematic 0.50 -> 0.60 — the same recall as raising
k to 10, without doubling the analyst's context.

### Two-signal relevance threshold

Absolute distance alone could not separate answerable from unanswerable, because the ranges
overlap. Adding the *shape* of the distance profile does:

| Rule | Negatives blocked | Positives lost |
| --- | --- | --- |
| distance <= 0.48 | 3/5 | 0 |
| gap >= 0.040 | 5/5 | 7 |
| **distance <= 0.48 AND gap >= 0.025** | **5/5** | **1** |

An unanswerable question returns k uniformly mediocre chunks — nothing stands out. An
answerable one has a clear winner. The gap between best and mean captures that; the
absolute distance discards it.

The one positive lost is `theme-delegate-incentives` (best 0.462, gap 0.013) — a genuinely
hard cross-protocol question where four DAOs use four different names for the same idea.
Losing it is the price of blocking five confident wrong answers, and it is the right trade
for a governance risk tool.

**These constants are fitted on 24 questions.** They are tuned parameters, not discovered
properties of the embedding space, and want re-validating as the eval set grows.

### The bug that fitting exposed

The first re-measurement gave 3/5 negatives clean, not the 5/5 the sweep predicted. Cause:
`min_gap` was fitted over 10 results but applied over the 20 that diversity over-fetches. A
longer tail raises the mean, which widens the gap for free, so two negatives sailed through
a threshold that the fit said would block them.

Fixed by measuring the gap over a fixed `GAP_WINDOW` of 10 regardless of `k` or the fetch
multiplier, making the signal independent of retrieval settings. A test pins it by
appending a mediocre tail and asserting the verdict does not change.

Worth noting how this surfaced: the sweep predicted 5/5, the measurement said 3/5, and the
disagreement was the bug. Without a recorded prediction there would have been nothing to
disagree with.

### Still outstanding

**Protocol bleed** is unfixed. Filters help when applied (`protocol=aave, source=proposal`
takes the failing question from 0/3 to 2/3) but knowing to apply them requires parsing the
question, which is the Phase 5 router's job. The Phase-4-only alternative — putting the
protocol name into the chunk text and re-embedding for ~$0.055 — remains untested.

**Thematic recall at 0.60** is the weakest number. Diversity helped; the remaining misses
are cross-protocol questions where vocabulary differs between DAOs.
