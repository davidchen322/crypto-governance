# Phase 5 — The Agent: Intent Router and Analyst Synthesis

**Status:** planned, not started · **Written:** 18 Aug 2026 · **Estimate:** ~2 evenings

Phase 4 ended with a function. You type a question, you get back five chunks of governance
text ranked by relevance. Phase 5 turns that into something that **answers**.

The original plan describes this in one dense paragraph of LangGraph jargon. This document is
that paragraph, unpacked.

---

## What Phase 5 actually is

Three things a person would recognise:

1. **A dispatcher** that reads your question and decides *where the answer lives* — the
   SQL tables, the semantic index, or both.
2. **Three fetchers**, one per destination, that go and get the material.
3. **A writer** that reads what came back and produces an analyst-quality answer with
   citations you can click through to the source.

Wired together as a small graph, so each piece is separately testable and the whole thing has
one entry point.

```
   question
      │
      ▼
  ┌────────┐   "counts, filters, dates"   ┌────────────┐
  │ ROUTER │ ──────────────────────────►  │ SQL node   │──► silver (Iceberg)
  │        │   "open-ended themes"        ├────────────┤
  │ reads  │ ──────────────────────────►  │ Vector node│──► pgvector
  │  only  │   "both"                     ├────────────┤
  │the text│ ──────────────────────────►  │ Hybrid node│──► both
  └────────┘                              └─────┬──────┘
                                                │ rows + chunks
                                                ▼
                                        ┌───────────────┐
                                        │   SYNTHESIS   │  analyst prompt,
                                        │  strong model │  structural citations
                                        └───────┬───────┘
                                                ▼
                                          answer + sources
```

---

## Why a router at all

Different questions have different *shapes*, and the shapes need different tools.

> *"How many Aave proposals passed in the last quarter?"*

This is arithmetic over structured fields. `proposal_versions` has `proposal_state`,
`voting_end`, `protocol_name` — a `SELECT count(*) … WHERE` answers it exactly. Semantic
search would be a terrible way to count things: it returns five chunks that *talk about*
proposals passing, and counting them gives you five.

> *"What are the arguments against protocol fee switches?"*

There is no column for "arguments against". This is what the embeddings are for.

> *"Which Aave proposals in the last quarter changed risk parameters, and what did the forum argue about them?"*

Both. A date filter and a protocol filter (SQL), then the qualitative debate (vector).

**The critical design constraint: the router reads the question and nothing else.** It never
touches the corpus. A router that had to query data in order to decide how to query data
would be circular and expensive. It sees a sentence, emits one label, and steps aside — which
is why it can run on a small cheap model while only synthesis needs a strong one.

---

## The pieces, one at a time

### The state object

Every node in the graph reads and writes one shared dictionary. Roughly:

```python
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    question: str
    route: Literal["sql", "vector", "hybrid"]
    filters: dict          # protocol, date range, source — extracted by the router
    rows: list[dict]       # what the SQL node found
    chunks: list[SearchResult]   # what the vector node found
    answer: str
    citations: list[str]
```

**The `add_messages` annotation is not decoration.** The blueprint had a docstring where this
belongs. Without a reducer, LangGraph *overwrites* `messages` on each node's return instead of
appending — so conversation history silently vanishes and every turn looks like the first. The
annotation tells the graph "merge these, don't replace them."

### The router node

Input: the question. Output: a label and any filters it can extract.

```json
{"route": "hybrid", "protocol": "aave", "since": "2026-05-01", "source": null}
```

Small model, temperature 0, JSON-constrained output. Filters are a bonus: if it confidently
sees "Aave", downstream retrieval can use `protocol="aave"` — Phase 4 measured that as taking
the failing V4 question from **0/3 to 2/3**.

### The SQL node

Translates the question into a query against silver via Trino. The tables are already there
and already queryable — `proposal_versions` (27 columns including `proposal_state`,
`voting_start/end`, `vote_count`, `scores_total`) and `forum_posts`.

Two hard rules:

- **`WHERE is_current` unless the question is historical.** Silver is SCD2, so without it a
  proposal edited three times is counted three times.
- **Never let the model emit raw SQL against a live connection.** Constrain it to a set of
  parameterised query templates — count, list, filter-by-date, top-N-by-field. A template
  covers the real question shapes and cannot be talked into `DROP TABLE`.

### The vector node

Calls `search()` exactly as Phase 4 left it. This is the layer that already works and is
already measured — the whole point of building it standalone first.

### The hybrid node

Runs both, and this is where the ordering matters: **SQL first, then vector within its
result.** Get the 14 Aave proposals from last quarter, then semantically search *within* that
set, rather than semantically searching everything and hoping the date filter is respected.

### The synthesis node

Takes the rows and chunks and writes the answer. Two things carry over verbatim from the
original blueprint because they are genuinely good:

**The three-dimension structure.** Governance questions have technical, economic, and
political dimensions, and an answer that covers all three is what makes it analyst-grade
rather than a summary. *"This changes the LTV parameter"* (technical), *"which reduces
borrowing capacity by ~$40M"* (economic), *"and the risk service provider proposed it over
delegate objections"* (political).

**The `DATA GAP IDENTIFIED` escape hatch.** An explicit instruction that when the retrieved
material does not support an answer, say so instead of inventing one. Phase 4 measured exactly
why this matters: one question retrieves a chunk about *revenue projections* for a question
about *founder compensation*, at a distance no threshold can reject. The synthesis step is the
last line of defence, and it is the one component that can actually read the text and notice.

### Citations that structurally cannot be faked

The instruction "cite your sources" is not enforcement. Instead, make citations part of the
**output contract**:

```json
{
  "answer": "Aave deprecated oracles for long-tail assets [c1], which delegates
             argued would strand positions [c2].",
  "citations": [
    {"marker": "c1", "chunk_id": "…", "document_id": "0xaa68…", "source": "proposal"},
    {"marker": "c2", "chunk_id": "…", "document_id": "25400",  "source": "forum"}
  ]
}
```

Then **validate after generation**: every marker in the prose must exist in the citation list,
and every `chunk_id` must resolve to a chunk that was actually retrieved for this question. A
hallucinated citation fails a lookup rather than reaching the user. That check is a unit test,
not a hope.

---

## What Phase 4 measured that changes this design

Four findings that should shape Phase 5 rather than be rediscovered inside it.

### 1. Set `k` by question shape — free, and the largest available win

Thematic misses are a **ranking** problem, not an absence problem:

| | k=5 | k=10 | k=20 | k=50 |
| --- | --- | --- | --- | --- |
| thematic recall | 0.78 | 0.83 | **0.94** | 1.00 |

Every expected document is already being retrieved; it just sits below the cut. Someone
asking *"which protocols pay delegates?"* expects a list, not five items. The router is the
first component that knows a question is thematic, so it is the right place to set `k=20`
while a lookup question keeps `k=5`.

Verified: negatives stay clean at **5/5** for k=5, 10 and 20, because the accept/reject
decision is computed on the raw top-10 profile regardless of `k`.

### 2. Use the model as a relevance judge, not a reranker

Measured, on the exact pair no threshold can separate:

| Question | Top relevance score | Passages scoring ≥2 |
| --- | --- | --- |
| *"How much is the Aave founder paid?"* (should return nothing) | **0** | **0** |
| `theme-delegate-incentives` (should return 4 documents) | **3** | **7** |

Total rejection and total confidence — a clean split where distance (`0.451` vs `0.458`) has
the two in the *wrong order*.

But the same model **reranking for order made thematic recall worse** (0.78 → 0.72). It
optimises "does this passage answer the question" and so prefers one deeply detailed
protocol over the spread of documents a cross-protocol question needs.

**So: judge, don't reorder.** Ask "does any of this actually address the question?" and let a
"no" trigger `DATA GAP IDENTIFIED`. Cost measured at ~$0.0004/query.

### 3. Protocol filtering belongs to the router

Phase 4 tested whether chunk text alone could fix protocol bleed (an Aave question returning
Uniswap results). It could not. The filter works — `protocol="aave"` takes the failing
question from 0/3 to 2/3 — but applying it requires parsing the question, and a wrong guess
excludes the right answer entirely. That is a decision that needs intent, which is what the
router produces.

### 4. Routing introduces a brand-new error source

Today a question is "thematic" because a human labelled it. In Phase 5 it is thematic because
a model guessed. **A misrouted question gets the wrong `k`, the wrong store, possibly the
wrong protocol filter** — and every Phase 4 number was measured without that error existing.

This is why the exit criteria name a routing accuracy target. Left unmeasured, misclassification
is precisely the kind of invisible failure this project keeps finding.

---

## The routing eval set — build it before the router

Fifteen questions labelled with the branch they *should* take. Same method as Phase 4: write
the measurement first, so "the router works" is a number.

It is genuinely new work, not a relabelling of `questions.yaml`. **The Phase 4 eval set
contains no SQL-shaped questions at all** — no counts, no date ranges, no "how many" — because
Phase 4 had no SQL branch to test. The two taxonomies are different axes:

| | Phase 4 kinds | Phase 5 routes |
| --- | --- | --- |
| Question | *What is it about, and is it answerable?* | *Which store can answer it?* |
| Values | lookup / thematic / cross_source / negative | sql / vector / hybrid |

A rough target shape: 5 SQL, 5 vector, 5 hybrid, with deliberate near-misses — *"how many
proposals mention risk parameters"* looks like a count but needs semantics, and belongs in
hybrid. Those boundary cases are where routing accuracy is actually decided.

---

## Build order

1. **Routing eval set** (15 labelled questions). Measurement first.
2. **State + graph skeleton** — nodes that return canned values, `.compile()`, terminal edges.
   Prove the graph runs end to end before any node is real.
3. **Router node** + measure against the eval set. Iterate the prompt until ≥80%.
4. **Vector node** — thin wrapper over `search()`, with `k` from the route.
5. **SQL node** — parameterised templates over silver, `is_current` enforced.
6. **Hybrid node** — SQL first, vector within its results.
7. **Synthesis node** + the citation validator.
8. **`gov ask "…"`** — terminal chat, mirroring `gov search`.
9. Re-run `make eval` to confirm Phase 4 retrieval has not regressed.

Steps 2 and 3 are where the blueprint's shipped code failed outright — it never called
`.compile()`, routed to three undefined nodes, and had no `END` edge, so the module raised on
import and the API never started. Doing the skeleton first makes that class of error
impossible to carry forward.

---

## How we will know it works

**Exit criteria, from the plan:**

- Router selects the correct branch on **≥80%** of the 15-query routing set.
- **Every factual claim carries a resolvable citation** — enforced by the validator, so a
  hallucinated `chunk_id` fails a lookup rather than reaching a user.

**Demo:** a terminal session answering *"which Aave proposals in the last quarter changed risk
parameters, and what did the forum argue about them?"* — a genuine hybrid question needing a
date filter, a protocol filter, and qualitative forum material, with citations resolving to
real chunks.

**Tests, in the Phase 4 style:**

| Test | Guards |
| --- | --- |
| `messages` accumulate across turns | the missing-reducer bug, which silently erases history |
| the graph compiles and every edge terminates | the blueprint's import-time crash |
| a hallucinated `chunk_id` is rejected | citations that look right and point nowhere |
| SQL templates always constrain `is_current` | SCD2 double-counting edited documents |
| a routed question reaches only its own store | a "vector" question quietly running SQL |
| `DATA GAP` fires when the judge scores everything 0 | confident answers from irrelevant text |
| `make eval` still passes | Phase 5 silently regressing Phase 4 retrieval |

---

## Cost

Per query, not per corpus:

| Component | Model | Rough cost |
| --- | --- | --- |
| Router | small, cheap | negligible — one short sentence in, JSON out |
| Relevance judge | small, cheap | ~$0.0004 (measured) |
| Synthesis | strong | the dominant per-query cost |
| Retrieval | — | one embedding call, fractions of a cent |

Embeddings were a **one-time** corpus cost. LLM calls are **per question**, so a loop or a
retry storm is the first thing in this project that can run up a bill. The existing provider
spend cap stays essential.

---

## Open decisions

- **Which model for synthesis.** Deliberately deferred — it is a one-client swap, and the
  router/judge economics do not depend on it.
- **Whether the relevance judge is its own node or part of synthesis.** A separate node is
  cleaner to test; folding it in saves a call. Lean separate, since it is the component with a
  measured number attached.
- **Conversation memory scope.** `add_messages` makes multi-turn possible; whether Phase 5
  actually exposes follow-up questions or stays single-shot is a product decision, not a
  technical one.

---

## What this phase does *not* do

No FastAPI (Phase 6), no dashboard, no scheduling, no contract source. Phase 5 ends at a
terminal command that answers a governance question with citations — the point at which the
plan notes *"working product from here."*
