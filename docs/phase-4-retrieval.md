# Phase 4 — Embeddings, Retrieval and the Baseline

**Status:** Baseline measured, all three findings addressed · **Date:** 17 Aug 2026

## Current numbers

| Metric | First baseline | After diversity + gap threshold | After chunk scheme v2 |
| --- | --- | --- | --- |
| micro recall@5 | 0.67 | 0.78 | **0.81** |
| macro recall@5 | 0.82 | 0.88 | **0.89** |
| lookup | 0.88 | **0.96** | 0.92 |
| thematic | 0.50 | 0.60 | **0.70** |
| cross_source | 1.00 | 1.00 | 1.00 |
| negatives clean | 0/5 | 5/5 | 5/5 |

The first two fixes cost nothing. The third cost $0.055 and is a **mixed result, not a
clean win** — it is kept on the strength of micro, macro and thematic, against a real
regression in lookup. That trade is argued in *Chunk scheme v2* below, and it is reversible
with one constant because both schemes are still in the table.

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

Then Phase 5 — the LangGraph router, which consumes `search()` as it now stands, and which
is where protocol filtering belongs (see *Chunk scheme v2* below for why text alone did not
solve it).

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

---

## Chunk scheme v2 — the protocol label in the text

Finding 3 offered two fixes for protocol bleed. A hard `WHERE protocol_name = ...` filter is
free but excludes the right answer outright when the guess is wrong, and breaks the
cross-protocol questions that legitimately span all five DAOs. The soft alternative — put
the label in the text the model actually sees — was chosen deliberately for that reason.

`chunk_proposal()` now prepends the protocol's prose label to the title:

```
v1   [ARFC] Oracle Deprecation for Long-tail Assets\n\nSummary\n\nLlamaRisk prop...
v2   Aave — [ARFC] Oracle Deprecation for Long-tail Assets\n\nSummary\n\nLlamaRisk prop...
```

`protocol_name` was always a column, and a column is invisible to a vector. This is the
smallest change that puts the fact into the embedding space.

### What it actually did

Three questions moved. That is the whole effect — the other 21 were unchanged.

| Question | Kind | v1 | v2 |
| --- | --- | --- | --- |
| `theme-protocol-fee-expansion` | thematic | 0.75 | **1.00** |
| `theme-delegate-incentives` | thematic | 0.50 | **0.75** |
| `aave-v4-deployment-targets` | lookup | 0.67 | **0.33** |

The gains landed exactly where the change was aimed: cross-protocol thematic questions,
where naming the DAO in every chunk gives a question like *"which DAOs pay delegates?"*
something to match against. Thematic was the weakest category and it improved most.

**The regression is the interesting part.** `aave-v4-deployment-targets` is the question
that motivated the change, and the change made it worse:

```
0.332  uniswap   Uniswap — [RFC] Four for V4 ...          <- still the top hit
0.351  aave      Aave — [ARFC] Aave V4 Activation on Ethereum Mainnet
0.371  aave      Aave — [ARFC] Deploy Aave V4 on Avalanche
0.374  uniswap   Uniswap — [Temp Check] - Four for V4 ...
0.380  aave      Aave — AL Development Update | July 2026
```

Uniswap's *Four for V4* is **still ranked first for an Aave question**, with the word
"Uniswap" sitting in its own chunk text. The hypothesis was that the label would break the
"V4" collision; measured, it does not. Both Uniswap chunks now carry a token the query
does not ask for, and both still outrank two of the three expected Aave proposals — one of
which lost its slot to an Aave *forum* post that gained from the same prefix.

So the honest reading is: the label is a useful **topical** signal for questions that range
across DAOs, and a weak **disambiguating** signal for questions that name one. Those are
different jobs, and only the first one worked.

### Why it is kept anyway

Micro +0.03, macro +0.01, thematic +0.10, against lookup −0.04 on a single question.
Thematic is the weakest category and the one with the most room; lookup at 0.92 is still
the strongest of the three. Negatives stayed clean at 5/5, so the extra shared vocabulary
did not loosen the cutoff.

It is worth being clear that this is a judgement call on a 24-question eval set, and a
one-question swing is inside the noise that set can resolve. The defensible part is not the
+0.03 — it is that **the decision is reversible and the evidence is recorded**. Both schemes
sit in `document_embeddings`; reverting is `CHUNK_SCHEME = "v1"` and costs nothing, because
the v1 vectors were never deleted.

### Protocol bleed is still the right job for the router

The measured result is that text alone does not fix it. What does work is the filter:
`protocol=aave, source=proposal` takes the same question from 0/3 to 2/3. The reason not to
apply it unconditionally is unchanged — a wrong guess excludes the answer entirely — which
makes it a decision that needs to see the parsed question, i.e. the Phase 5 router. v2 makes
that router's job easier without pretending to replace it.

---

## Bugs the re-embed exposed in the tests

Doubling the table from 1,655 to 3,310 rows broke three integration tests. All three were
tests that had been passing for the wrong reason.

**`test_no_duplicate_chunks_for_one_model`** grouped on `(source_content_hash, chunk_index,
embedding_model)` and reported all 1,655 v1 rows as duplicates. The grouping was missing
`chunk_scheme` for the same reason the UNIQUE constraint originally was: the same source
text chunked two ways is two legitimate rows.

**`test_hnsw_index_exists_and_is_used`** asserted the planner *chose* the HNSW index. At
3,310 rows it correctly stops choosing it — HNSW costs ~1,888 to start against ~594 to sort
the whole table. Nothing was wrong; the test was measuring table size. It now forces
`enable_seqscan = off` and asserts the *shape* of the plan, which still catches the failure
worth catching (a wrong operator class leaves the index unusable and falls back to a Sort).

**`test_threshold_rejects_distant_matches`** passed `max_distance=None` for its control arm
but left `min_gap` at its default, so the "unfiltered" search was still filtered. It passed
only while that question's distance profile happened not to look flat. Under v2 it does look
flat — correctly, since it is a negative — and the control returned nothing. Now both
cutoffs are disabled explicitly.

None of these were caused by v2. They were latent, and a change in corpus size was enough to
surface them — the same pattern as the `min_gap` window bug: the test agreed with the code
by coincidence, and it took new data to notice.

---

## The `gov` CLI and the Phase 4 exit demo

Building the demo the plan specifies (`gov search "..."`) surfaced three findings and one
check that mattered more than any of them — including confirmation that the relevance
cutoffs, fitted on v1, are **still valid under v2**: the re-embed pulled every positive
category closer (lookup −0.032, thematic −0.028, cross_source −0.018) while leaving the
negatives unmoved (+0.000).

That is a better argument for keeping v2 than the mixed recall result above, and it is
written up in full, with reproduction steps, in
**[phase-4-exit-demo.md](phase-4-exit-demo.md)**.

---

## Tests added for the scheme mechanism

| Test | What it pins |
| --- | --- |
| `test_both_chunk_schemes_are_stored` | rollback stays a one-constant change; the paid-for v1 vectors are not discarded |
| `test_uniqueness_includes_the_chunk_scheme` | without it the loader's skip check reports "already embedded" and serves stale vectors |
| `test_v2_chunks_carry_the_protocol_label` | the label is in the text, not just the column |
| `test_search_returns_only_the_active_scheme` | ranking across two schemes compares vectors built from different text, which is meaningless |
