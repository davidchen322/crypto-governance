# Phase 4, Explained

**A plain-language guide to what was built, what every number means, and why each fix
happened.** The other Phase 4 documents record decisions as they were made; this one assumes
you are reading them cold.

---

## What Phase 4 actually built

Phase 3 left the governance text sitting in Iceberg tables — correct, versioned, and
completely unsearchable except by SQL keyword matching. Phase 4 makes it searchable *by
meaning*.

Four pieces:

1. **Chunking** (`chunking.py`) — a 4,000-word proposal is too big to search as one unit, so
   it is split into ~400-token pieces along its markdown headings. Ask about "risk
   parameters" and you should get the Specification section, not the whole document.
2. **Embedding** (`embeddings.py`) — each chunk is sent to OpenAI and comes back as a list of
   1,536 numbers (a *vector*) that encodes its meaning. Chunks about similar things get
   similar numbers. This is the part that costs money: ~$0.055 for the whole corpus.
3. **Retrieval** (`retrieval.py`) — your question is turned into a vector the same way, and
   Postgres finds the chunks whose vectors sit closest to it.
4. **The eval set** (`tests/eval/`) — 24 hand-written questions with the documents that
   *should* come back, so "did that change help?" has an answer that is not a shrug.

The number that measures closeness is **cosine distance**: `0.0` is identical, `1.0` is
unrelated, `2.0` is opposite. In this corpus a good match is around **0.30–0.45**, and
genuine nonsense lands at **0.80+**. Every metric below is built on that one number.

---

## The 24 questions, and why they come in four kinds

The eval set is not 24 interchangeable questions. Each kind probes a different failure.

| Kind | n | Example | What it catches |
| --- | --- | --- | --- |
| **lookup** | 8 | *"Which Aave proposal deprecates Chainlink price feeds?"* | Can it find one specific known document? |
| **thematic** | 5 | *"Which protocols are running programs to pay delegates?"* | Can it gather several documents across DAOs on one theme? |
| **cross_source** | 6 | *"What is the proposed next era of ENS DAO governance?"* | Can it connect a proposal to its forum discussion? |
| **negative** | 5 | *"What is the current price of UNI?"* | Does it know when to say **nothing**? |

The negatives are the ones people skip, and they are the most important. Vector search
*always* returns results — something is always closest — so without a deliberate cutoff, a
question the corpus cannot answer still comes back with five confident-looking chunks. An
analyst then writes a confident, wrong answer citing them.

---

## What each metric means

### recall@5

> Of the documents that *should* have come back, how many appeared in the top 5?

If a question expects 4 documents and 3 show up, recall is `3/4 = 0.75`.

**@5** is the cutoff: only the top 5 results count. That is not arbitrary — it is roughly
what fits in an analyst's attention and, later, in a language model's context.

Recall is scored at the **document** level, not the chunk level. If the label says "Aave's
oracle proposal" and retrieval returns that proposal's *Motivation* section while the label
was written against its *Summary*, that counts as a hit. It found the right document; which
paragraph it surfaced is not a real failure.

### micro vs macro — the same data, weighted two ways

These differ only in what gets averaged, and the gap between them is informative.

Take three questions:

| | expects | found | recall |
| --- | --- | --- | --- |
| Q1 | 1 doc | 1 | 1.00 |
| Q2 | 1 doc | 1 | 1.00 |
| Q3 | 4 docs | 1 | 0.25 |

- **micro** = all documents found ÷ all documents expected = `3 ÷ 6` = **0.50**
  Every *document* counts equally, so hard multi-document questions dominate.
- **macro** = mean of the per-question recalls = `(1.00 + 1.00 + 0.25) ÷ 3` = **0.75**
  Every *question* counts equally, so easy questions can mask a hard one.

**macro is always the flattering number.** When macro sits well above micro — as it does
here, 0.90 vs 0.83 — it means the easy single-document questions are working and the
multi-document thematic ones are dragging. That is exactly the situation, and reporting only
macro would hide it.

### negatives clean

> Of the 5 unanswerable questions, how many correctly returned **nothing**?

`5/5` means the cutoffs held on all five. `0/5` — the first baseline — means every single
one returned five confident chunks about nothing.

### false rejections *(added most recently)*

> Of the 19 answerable questions, how many survive the cutoffs?

This one exists because of a blind spot. Recall is deliberately measured with the cutoffs
**off**, so it grades the *ranking*. That means a cutoff which wrongly silences a perfectly
answerable question costs **nothing** on the recall line — only the 5 negatives ever exercised
the thresholds at all.

Adding it immediately caught a real bug (below). It is the counterweight to "negatives clean":
one measures over-answering, the other measures over-refusing, and tightening either one
degrades the other.

### Why recall and negatives are measured under different settings

This surprises people, so it is worth stating plainly:

- **Recall** is measured with the cutoffs **OFF** — it grades the ranking. Is the right
  document *near the top*?
- **Negatives** are measured with the cutoffs **ON** — they grade the cutoff. Does anything
  survive when it shouldn't?

Blend them into one score and a sloppy cutoff hides inside decent recall. They are separate
because they are separate questions.

---

## Why the table has columns: each one is a snapshot after a fix

Each column is the same 24 questions, re-run after a change. That is the whole point of
having a recorded baseline — it converts "this feels better" into a number that can move the
wrong way.

| Metric | First baseline | After diversity + gap threshold | After chunk scheme v2 | After curation |
| --- | --- | --- | --- | --- |
| micro recall@5 | 0.67 | 0.78 | 0.81 | **0.83** |
| macro recall@5 | 0.82 | 0.88 | 0.89 | **0.90** |
| lookup | 0.88 | **0.96** | 0.92 | 0.92 |
| thematic | 0.50 | 0.60 | 0.70 | **0.75** |
| cross_source | 1.00 | 1.00 | 1.00 | 1.00 |
| negatives clean | 0/5 | 5/5 | 5/5 | **5/5** |
| false rejections | *not measured* | *not measured* | *not measured* | **1/19** |

### Column 1 — "First baseline": the corpus embedded, nothing tuned

Chunking, embedding and `search()` built as specified, then measured. micro **0.67**,
thematic **0.50**, negatives **0/5**.

Those numbers are bad, and that was the point — you cannot tell improvement from change
without a starting line.

### Column 2 — "After diversity + gap threshold": two distinct problems

**Problem A: chunk domination.** The top 5 chunks contained, on average, only **2.0 distinct
documents**. A long proposal splits into 30 chunks, several score well, and they crowd out
everything else:

```
1. Aave V4 Activation — chunk 3     <- same
2. Aave V4 Activation — chunk 7     <- document
3. Aave V4 Activation — chunk 12    <- three times
4. Uniswap Four for V4 — chunk 1
5. Uniswap Four for V4 — chunk 4
```

A question expecting four documents **cannot** succeed here regardless of ranking quality —
two documents occupy all five slots. This is why thematic sat at 0.50.

*Fix — diversity-aware retrieval:* fetch 20 chunks, keep at most **one per document**, return
the best 5 documents. Costs nothing, no re-embedding. thematic 0.50 → 0.60, lookup 0.88 → 0.96.

**Problem B: no way to say "I don't know".** All five negatives leaked. The obvious fix —
tighten the distance cutoff — does not work, because the ranges overlap:

```
answerable questions,   best distance:  0.215 … 0.462
unanswerable questions, best distance:  0.431 … 0.521
                                        ^^^^^^^^^^^^^ overlap
```

No single distance separates them.

*Fix — add a second signal, the shape of the results.* An unanswerable question returns five
*uniformly mediocre* chunks; nothing stands out. An answerable one has a clear winner. The
**gap** — how much closer the best hit is than the average of the top 10 — captures that.

| Rule | negatives blocked | positives lost |
| --- | --- | --- |
| distance ≤ 0.48 alone | 3/5 | 0 |
| gap ≥ 0.040 alone | 5/5 | 7 |
| **both together** | **5/5** | **1** |

Neither works alone. Together: 0/5 → 5/5.

### Column 3 — "After chunk scheme v2": the embedding could not see which DAO

**Problem: protocol bleed.** An **Aave** question about V4 returned **Uniswap's** "Four for
V4" as its top hits. "V4" is shared vocabulary across DAOs, and nothing in the chunk *text*
disambiguated it.

The protocol name was always in the database as a column — but **a column is invisible to a
vector**. The embedding only ever sees the chunk's text. So the text now leads with the
protocol:

```
v1   [ARFC] Oracle Deprecation for Long-tail Assets | Summary | LlamaRisk proposes…
v2   Aave — [ARFC] Oracle Deprecation for Long-tail Assets | Summary | LlamaRisk proposes…
     ^^^^
```

The alternative was a hard filter (`WHERE protocol_name = 'aave'`), rejected deliberately: a
filter that guesses wrong excludes the right answer outright, and it breaks cross-protocol
questions like "which DAOs pay delegates?" that legitimately span all five. A text prefix only
*nudges* the ranking.

**Result: mixed, and kept anyway.** thematic 0.60 → 0.70, but lookup 0.96 → **0.92** — and the
V4 question that motivated the whole change got *worse*. The prefix turned out to be a good
**topical** signal for questions ranging across DAOs and a weak **disambiguating** signal for
questions naming one.

The stronger argument for keeping it came from a different measurement: v2 moved every
answerable category *closer* to the corpus (lookup −0.032, thematic −0.028) while leaving the
unanswerable ones exactly where they were (+0.000). Better separation, not just a better score.

### Column 4 — "After curation": the corpus contained junk

Prompted by asking the system *"how many cats climbed up my tree?"*. It correctly returned
nothing — but the nearest match was Discourse's **default "Welcome to Discourse" topic**, a
thread the forum software creates automatically, full of one-line replies like *"You need
$ARB tokens"*.

Those tiny chunks act as **attractors**. On a 14-token chunk the `"Arbitrum — Welcome to
Discourse"` prefix is ~40% of the tokens, so the vector is mostly *protocol name* and sits
near any query mentioning Arbitrum. One of them caused a genuinely wrong answer to pass both
cutoffs.

*Fix — two curation rules:* drop platform boilerplate by title, and drop chunks whose body
falls below 20 tokens (with an exemption for proposals, where a terse document is still a real
governance act). 86 rows removed, no re-embedding.

thematic 0.70 → **0.75**, micro 0.81 → **0.83**.

**Plus a real bug the new "false rejections" metric caught immediately.** `cross-ens-next-era`
scored recall **1.00** — perfect — yet the cutoffs silenced it entirely. Its profile:

```
0.345 0.347 0.354 0.358 0.360 0.366 0.367 0.368 0.368 0.369
```

Every hit is excellent. But they are all *equally* excellent, so the gap is tiny and the
flatness rule read it as "nothing stands out". **Flatness cannot distinguish "nothing is
relevant" from "everything is relevant."** In production, a question that retrieved perfectly
returned nothing at all.

*Fix:* skip the flatness test when the best match is unambiguously close (≤ 0.42). Across 24
eval questions and 17 adversarial probes, no unanswerable question ever came nearer than
0.432, so the exemption is safe. False rejections 2 → 1, negatives still 5/5.

---

## Why "chunk scheme" is versioned at all

`chunk_scheme` is part of a row's identity, alongside `embedding_model`. Two reasons:

**1. It stops a silent staleness bug.** The loader decides what to re-embed by comparing the
*source content hash* — a hash of the document's text. Change how text is *chunked* and that
hash does not move. Without a scheme in the key, the loader reports "already embedded,
nothing to do" and serves vectors built by the old chunker forever. Exactly this trap let a
stale Phase 3 derivation survive a successful-looking rebuild.

**2. It makes changes reversible.** v1 and v2 coexist in the table. Reverting is one constant
and costs nothing, because the v1 vectors were never deleted. That is also what made the
v1-vs-v2 separation measurement possible at all — both vector spaces were still there to
compare.

Vectors from different schemes are never mixed in one search, for the same reason vectors from
different *models* are never mixed: they encode different text, so ranking across them is
meaningless.

---

## How to read the current numbers honestly

**Working well:**
- `cross_source` **1.00** throughout — connecting a vote to its debate is the thing the
  platform exists to do, and it has never failed.
- `lookup` **0.92** — finding a specific known document is reliable.
- negatives **5/5**, and outright nonsense is rejected by a wide margin (0.80–0.91 against a
  0.48 ceiling), so there is no tuning risk at that end.

**Still weak:**
- `thematic` **0.75** is the lowest. Cross-protocol questions are genuinely hard: four DAOs
  use four different words for the same idea.
- **1 false rejection** — `theme-delegate-incentives` (best 0.458) sits inside the overlap
  band where answerable and unanswerable questions are not separable by these two signals.
- **1 known false accept** — *"How much is the Aave founder paid?"* returns a chunk about
  revenue projections. It sits at 0.451 with a healthy gap, i.e. deeper in the overlap than
  some real questions. No threshold fixes this; it needs a reranker or a synthesis step
  willing to say "the retrieved text does not answer this".

**The honest summary:** the system is good at finding specific documents, decent at gathering
themes, reliable at refusing nonsense, and imperfect in a narrow band where plausible-sounding
unanswerable questions look exactly like hard answerable ones. That band is documented rather
than tuned away, because tuning it in either direction just trades one error for the other.

---

## Where to go next

- [`phase-4-retrieval.md`](phase-4-retrieval.md) — the decisions as they were made, with the
  measurements behind each
- [`phase-4-exit-demo.md`](phase-4-exit-demo.md) — building the CLI, and the three bugs that
  surfaced
- [`phase-4-corpus-curation.md`](phase-4-corpus-curation.md) — the cats question and what it
  exposed
- [`chunking.md`](chunking.md) — how documents are split, in detail

```bash
make search Q="oracle deprecation for long-tail assets"   # try it
make eval ARGS="--verbose"                                 # reproduce every number above
```
