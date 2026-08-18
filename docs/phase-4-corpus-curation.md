# Corpus Curation — What "How Many Cats Climbed Up My Tree?" Exposed

**Status:** fixed, measured, tested · **Date:** 18 Aug 2026

New to these numbers? [`phase-4-explained.md`](phase-4-explained.md) defines every metric
used here.

---

## Where this started

A throwaway question: *what happens if I search something ridiculous?*

```
$ gov search "how many cats climbed up my tree?"
"how many cats climbed up my tree?"  —  no sufficiently relevant chunks
```

Correct. And off-domain questions turn out to be the *easy* case — they are rejected on
distance alone, by a wide margin:

| query | best distance | ceiling |
| --- | --- | --- |
| how many cats climbed up my tree? | 0.803 | 0.48 |
| what is the best recipe for lasagna? | 0.893 | 0.48 |
| who won the 1998 World Cup? | 0.909 | 0.48 |
| *(a real answerable question)* | ~0.450 | 0.48 |

There is no ambiguity there and no tuning risk. But the *nearest match* to a question about
cats was Discourse's default "Welcome to Discourse" thread — and that was worth pulling on.

---

## Finding 1 — a false accept, found by probing the boundary

The dangerous region is not nonsense; it is questions that **sound** answerable and are not.
Probing there produced a genuine failure:

```
$ gov search "How much is the Aave founder paid?"
1. Aave  proposal  2026-07-21  distance 0.451
   [ARFC] Aave App Launch      section: 3. Revenue Opportunities
   ... projected to generate over $15M in annual revenue by end of year two ...
```

Revenue projections offered as an answer about founder compensation. "Aave" plus money
vocabulary is enough to clear both cutoffs.

To find out whether this was a one-off, I ran 15 phrasings across all five negative
categories — the eval set's exact wording plus two near-synonyms each. **13 blocked, 2 leaked.**

Two things fell out of that sweep that the eval set alone would never have shown:

- **The gap signal is load-bearing.** The exact `neg-founder-salary` (best 0.470) and
  `neg-compound` (0.439) both pass the *distance* test and are caught only by flatness.
- **The eval set's 5/5 is measured on five specific sentences.** Near-synonyms of the same
  questions behave differently. 5/5 is a real result, but it is narrower than it looks.

---

## Finding 2 — the corpus contained attractors

The second leak pointed straight at the cause:

```
$ gov search "What's the market cap of the ARB token?"
1. Arbitrum  forum  2023-04-02  distance 0.427
   Welcome to Discourse
   Arbitrum — Welcome to Discourse You need $ARB tokens
```

A **14-token chunk**. `forum:7` is Arbitrum's copy of the topic Discourse auto-creates on
every install, accumulated with one-line replies. Measured:

- **19 chunks**, averaging **65 tokens** against **259** for the rest of the corpus
- **never a correct answer** anywhere in the eval set — pure noise
- top hit for the cats question, for "delegate voting power concentration", *and* for the
  market-cap question

**Why chunk scheme v2 made this worse specifically.** Every chunk is prefixed with
`"Arbitrum — Welcome to Discourse"`, roughly 8–12 tokens. On a 400-token chunk that is noise;
on a 14-token chunk it is ~40% of the content. The vector becomes mostly *protocol name*, so
it sits near any query mentioning Arbitrum regardless of subject. The prefix that helped
thematic recall also amplified the junk it was applied to.

---

## Finding 3 — a hash collision hiding behind the junk

Chasing the short chunks turned up something worse. One surviving chunk was **4 tokens** —
just a title, no body — and its `content_hash` was:

```
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
= sha256("")
```

**Every empty-bodied post hashes identically.** Silver has 9 of them, across 4 protocols and
6 distinct topics:

```
optimism  topic    55  'Working Constitution of the Optimism Collective'
ens       topic  5371  'Governance Process'                      (x3)
uniswap   topic  5142  'Uniswap Governance Forum Rules'          (x2)
uniswap   topic 19976  'Community Governance Process Update…'
uniswap   topic 26229  '[RFC] Web4 Autonomous State Engines…'    (x2)
```

The UNIQUE constraint is `(source_content_hash, chunk_index, embedding_model, chunk_scheme)`,
so all nine collapse into **one** stored row — which keeps whichever title and `document_id`
was inserted first. A retrieval hit on that row would cite **ENS** for a chunk equally
representing a **Uniswap** post.

The practical harm is small (the text is empty, so there is little to mis-cite) but the
mechanism is a genuine correctness bug: the two-hash design assumes `content_hash` identifies
text, and for empty text it identifies nothing. The fix is not to make empty text hash
uniquely — it is to never embed a document with no body, since there is nothing to retrieve.

---

## What was changed

### Two curation rules

Both live in `config/corpus_filters.py`, applied by the loader so junk is never embedded, and
both are *reported* rather than applied silently:

```
curation: skipped 20 boilerplate post(s), 52 chunk(s) under 20 body tokens
```

**Rule 1 — platform boilerplate**, matched on **title, not topic id**. Discourse's default
topic ids differ per install, so an id-based denylist would be right for exactly one forum
and silently wrong for the rest.

**Rule 2 — a 20-token floor on body text**, where *body* explicitly excludes the
protocol/title prefix. That distinction is the whole point: the prefix is identical across
every chunk of a document, carries no per-chunk information, and is exactly what inflates a
runt chunk past a naive token check. A new `Chunk.body_tokens` field carries it.

### The exemption, and getting it wrong first

A floor risks deleting real governance history, so single-chunk documents are exempt — the
chunk *is* the document.

**Applied to forum posts, that exemption was wrong**, and the eval set said so. Each forum
*post* is its own document, so a one-line reply is a single-chunk document and got protected.
That re-admitted:

```
"Thank you for the support!"          "Stay Optimistic! The best community"
"I will join Optimism Collective"     "Great start. Excited to add value"
```

and cost **0.05 of thematic recall** (0.75 → 0.70). The exemption is now asymmetric, for a
stated reason:

- **Proposals** — exempt. A terse proposal is a governance act with a vote attached, the eval
  set has lookup questions against short ones, and nothing else stands in for it.
- **Forum posts** — not exempt. A one-line reply is not a governance act, and its topic stays
  fully retrievable through the posts that carry substance.

A chunk with **no body at all** is never exempt, which is what removes the `sha256("")` rows.

### The gap exemption — a bug the new metric caught immediately

I also added a **false rejections** measurement to `score.py`, because recall is scored with
the cutoffs *off*, so a cutoff that wrongly silences an answerable question had been costing
nothing. It found a real bug on its first run.

`cross-ens-next-era` scored recall **1.00** and was silenced completely:

```
0.345 0.347 0.354 0.358 0.360 0.366 0.367 0.368 0.368 0.369
```

Every hit excellent — and therefore *flat*. **Flatness conflates "nothing is relevant" with
"everything is relevant."** In production, a question that retrieved perfectly returned
nothing.

Fixed with `GAP_EXEMPT_DISTANCE = 0.42`: below that distance the flatness test is skipped
entirely. Safe because across 24 eval questions and 17 adversarial probes, the closest any
unanswerable question came was **0.4320**. Verified it opened no hole — the leak count was
identical before and after.

### Two smaller fixes

- **`prune_corpus.py` keyed on `(hash, index)`**, which let a stale ENS row survive because an
  unrelated Uniswap post shared its hash. Now keyed on `(source, document_id, hash, index)`.
- **`baseline.json` recorded no provenance.** Two runs with identical micro recall could have
  measured different vector spaces entirely. It now records `embedding_model`, `chunk_scheme`
  and `corpus_chunks`.

---

## Results

| Metric | Before | After |
| --- | --- | --- |
| micro recall@5 | 0.81 | **0.83** |
| macro recall@5 | 0.89 | **0.90** |
| thematic | 0.70 | **0.75** |
| lookup | 0.92 | 0.92 |
| cross_source | 1.00 | 1.00 |
| negatives clean | 5/5 | 5/5 |
| false rejections | *not measured* | **1/19** |
| adversarial probes leaking | 2/15 | **1/18** |
| corpus chunks (v2) | 1,655 | 1,593 |

The ARB market-cap leak was fixed by the curation alone — 0.427 → 0.502 once the boilerplate
chunk was gone, with no threshold change.

**Cost: $0.0001.** No re-embedding — 806 tokens to restore 30 chunks during the exemption
correction. Every existing vector was reused.

### What is still broken

**One false accept remains:** *"How much is the Aave founder paid?"* at 0.451 with a healthy
gap of 0.049. It cannot be separated by threshold tuning — the genuine positive
`theme-delegate-incentives` sits at 0.458 with a *worse* gap of 0.020, so any rule that blocks
the negative also blocks the positive. This needs a reranker or a synthesis step willing to
say "the retrieved text does not answer this". Documented rather than tuned away.

---

## Tests

**19 new test functions**, and the pre-existing suites still pass.

| File | Functions | Collected | Needs |
| --- | --- | --- | --- |
| `tests/test_phase4_curation.py` | 12 | 31 | nothing |
| `tests/test_phase4_embeddings.py` (gap exemption) | 3 | 3 | nothing |
| `tests/integration/test_phase4_retrieval.py` (curation) | 4 | 4 | Postgres |

The curation file collects more cases than it has functions because the boilerplate rules are
parametrized — 12 titles that must match and 9 that must not.

Suite totals: **126 unit, 74 integration** (21 of those `live`), all passing.

The ones carrying weight:

- **`test_real_governance_threads_are_not_swept_up`** — the expensive failure mode. *"About
  the treasury diversification proposal"* must survive a pattern aimed at *"About the …
  category"*, and *"Guidelines for delegate compensation"* must survive one aimed at a bare
  *"Guidelines"*. A curation filter slightly too aggressive deletes governance history and
  reports success.
- **`test_a_short_forum_reply_is_not_exempt`** — pins the asymmetry, with the measured cost of
  getting it wrong in the docstring.
- **`test_an_empty_body_is_never_substantive_even_when_exempt`** — the `sha256("")` path.
- **`test_a_densely_covered_question_is_not_treated_as_unanswerable`** — the flatness bug,
  with a fixture asserted to actually be flat so it cannot pass vacuously.
- **`test_the_corpus_matches_what_the_loader_would_produce`** — a drift check. If the stored
  corpus and the loader disagree, the next `make embed` silently changes retrieval results and
  every recorded number becomes unattributable.

---

## What this says about method

The eval set found none of this. It reported 5/5 negatives clean throughout, and it was right
— on the five sentences it contains.

What found it was **poking at the system by hand and following what looked odd**: a silly
question surfaced a junk chunk, the junk chunk surfaced a hash collision, and the metric added
to check the junk chunk immediately caught an unrelated bug that had been silencing a
perfect-recall question in production.

Two things worth carrying into Phase 5:

1. **A benchmark measures what it contains.** 24 questions cannot cover a corpus of 1,593
   chunks. Adversarial probing around the boundary is a different instrument, not a
   substitute — and it should be run deliberately rather than by accident.
2. **Every metric has a blind spot shaped like its own definition.** Recall-with-cutoffs-off
   could not see false rejections, so false rejections went unmeasured for three iterations.
   When adding a guard, ask what the guard's own failure would look like on the dashboard.
