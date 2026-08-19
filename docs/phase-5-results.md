# Phase 5 — The Agent: What Was Built, What Broke, What Was Decided

**Status:** exit criteria met · **Date:** 18 Aug 2026

Phase 4 ended with a function returning five ranked chunks. Phase 5 turns that into something
that answers, with citations that resolve. New to the metrics? See
[`phase-4-explained.md`](phase-4-explained.md). The plan this follows is
[`phase-5-plan.md`](phase-5-plan.md).

---

## Headline

| Exit criterion | Target | Measured |
| --- | --- | --- |
| Router selects the correct branch | ≥ 0.80 | **0.93** |
| Scope (one protocol / all) correct | — | **1.00** |
| Every factual claim carries a resolvable citation | enforced | **enforced by validator** |
| Phase 4 retrieval not regressed | unchanged | **unchanged** (micro 0.83, negatives 5/5) |

```bash
make eval-routing ARGS=--verbose        # score the router
make ask Q="How many proposals does each protocol have?"
make ask Q="Which Aave proposals in the last quarter changed risk parameters, \
and what did the forum argue about them?" ARGS=--explain
```

---

## What was built

```
ai_agent/graph/state.py           AgentState + the add_messages reducer
ai_agent/graph/workflow.py        topology, compiled; nodes injected so it is testable alone
ai_agent/graph/router.py          intent router — route AND scope
ai_agent/graph/llm.py             minimal chat client over the project's existing HttpClient
ai_agent/graph/sql_templates.py   parameterised SQL the model may run; it never emits SQL
ai_agent/graph/nodes.py           sql / vector / hybrid execution nodes
ai_agent/graph/synthesis.py       relevance judge, analyst prompt, citation validation
ai_agent/cli.py                   `gov ask`
tests/eval/routing.yaml           15 questions labelled with the branch they should take
tests/eval/score_routing.py       routing accuracy scorer
```

Built in the plan's order: **eval set first, then a skeleton that compiles, then real nodes.**
The skeleton mattered — the blueprint's version of this file never called `.compile()`, routed
to three undefined nodes, and had no `END` edge, so it raised on import.

---

## The demo

```
$ gov ask "Which Aave proposals in the last quarter changed risk parameters,
           and what did the forum argue about them?" --explain

  route: hybrid/one  {'protocol': 'aave', 'since': '2026-05-01'}

  In the last quarter, several Aave proposals changed risk parameters. '[ARFC] Low
  Adoption Asset Deprecation on Aave V3' aimed to reduce Aave's risk surface by
  deprecating assets whose usage had declined [c2]. '[ARFC] Umbrella Parameter Update'
  proposed to sunset GHO within the Umbrella by reducing target liquidity and emissions
  to zero [c3]. '[ARFC] Oracle Deprecation for Long-tail Assets' recommended deprecating
  Chainlink price feeds for assets that had lost adoption, affecting $6.76M supply and
  $4.29M debt [c4]. Forum discussion highlighted the need to manage risk and optimise
  resource allocation [c0].

  sources
   [c2] aave proposal  [ARFC] Low Adoption Asset Deprecation on Aave V3
       proposal:0x4ec0c13b…#1  distance 0.4383
   [c3] aave proposal  [ARFC] Umbrella Parameter Update: Target Liquidity and E
       proposal:0x1b262e62…#5  distance 0.4567
   [c4] aave proposal  [ARFC] Oracle Deprecation for Long-tail Assets Across Aa
       proposal:0xaa683250…#0  distance 0.4631
   [c0] aave forum     AL Development Update | July 2026
       forum:25482#2  distance 0.3864

  ran template 'list_proposals'; searched aave with k=20; narrowed proposals to the SQL
  result (19/19 chunks kept, 6 of them forum)
```

A genuine hybrid answer: a date bound and protocol filter from SQL, qualitative material from
the vector store, four citations that resolve, and cross-source evidence (three proposals plus
a forum thread).

**It did not look like that on the first run.** Every version below is a bug the demo exposed.

---

## Seven bugs, all of which produced fluent, confident, wrong output

None of these raised an exception. Each one is the kind that ships.

### 1. Every citation was unresolvable

The prompt showed passages as `[0] ref=proposal:0xaa68…#3` and asked the model to echo the
`ref`. It cited `"0"` — the bracket index. The answer was authoritative, well-structured, and
**every single citation failed to resolve.**

*Fix:* the model no longer supplies a source identity at all. Passages are labelled `[c0]`,
`[c1]`, and the model selects a marker from that list. A fabricated citation now fails a
dictionary lookup. Asking a model to reproduce an opaque string was an avoidable failure mode
when it could just pick a label.

### 2. Hybrid narrowing could never happen

`list_proposals` selected `title` but not `proposal_id`, so the hybrid node had no identities
to narrow the vector pass to. The date filter was decorative — the retrieval note said
*"SQL returned 2 titled row(s) but no ids, so no narrowing"*, which only appeared because the
node was written to report it.

*Fix:* the template selects `proposal_id`.

### 3. The router excluded half the evidence it was asked for

For *"…and what did the forum argue about them?"* the router set `source: "proposal"`. That
filter excluded every forum post — while the answer went on to describe "forum discussions".

*Fix:* the prompt now states that a question mentioning both sources must leave `source` null,
because setting it removes the other half of the evidence outright.

### 4. "The last quarter" resolved to 2023

The router had no notion of *today*, so it invented a plausible-looking date:
`since: 2023-07-01`, on a corpus whose newest proposal is from August 2026.

*Fix:* today's date is interpolated into the prompt. A live test asserts the resolved year is
within one year of now, so a regression fails rather than quietly retrieving the wrong window.

### 5. Narrowing deleted all forum evidence

Restricting the vector results to the SQL result's ids sounds right — but SQL templates query
`proposal_versions`, so those ids are *proposal* ids and **no forum topic can ever match one**.
Narrowing by that set silently deleted every forum chunk. The answer said *"specific arguments
from the forum are not provided in the evidence"* — for a question that asked precisely that.

*Fix:* narrow only the source the constraint ranged over. Proposals are filtered by the id set;
forum chunks pass through.

### 6. Counting questions refused to answer

A pure-SQL question routed correctly, ran the right template, got the right rows — and returned
`DATA GAP IDENTIFIED`. The citation contract was built around text passages and demanded a
marker on every claim. With no passages to cite, the model correctly concluded it had no
citable evidence.

*Fix:* the analyst prompt now distinguishes two kinds of evidence. **Structured rows are
authoritative and need no marker** — their provenance is the query, which `--explain` shows.
Passages still require one. A design gap, not a prompt tweak: I built one provenance model and
applied it to two branches.

### 7. Answers claimed to be truncated when they were not

`_snippet` (from Phase 4) decided truncation by comparing `len(" ".join(words))` against the
summed line lengths. That loses one space per line break, so **every multi-line text was
marked `...`**. Harmless in a search listing; on an answer it implies the analyst said more
than is shown.

*Fix:* track truncation explicitly. Pinned by a test asserting content round-trips unchanged.

---

## Two eval-set errors, found by running it

The routing set was written before the router, which is the point — but writing a benchmark is
itself error-prone, and the first run exposed two bad items rather than two bad answers.

**`route-oracle-deprecation` — my label was wrong.** I labelled *"Why were Chainlink price feeds
deprecated for long-tail assets?"* as `scope: one`, reasoning it is an Aave thread. But the
question names no protocol. For the router to know it is Aave-only it would have to read the
corpus — which it must never do. The label demanded knowledge the router structurally cannot
have. Corrected, and the rule written down: **`scope: one` requires a protocol identifiable
from the question text alone.** An integration test now enforces that across the whole set.

**`route-active-what-about` — the item was ambiguous.** *"What are the currently active
proposals about?"* was labelled hybrid, but `title` **is** a column, so `SELECT title WHERE
proposal_state='active'` genuinely answers it. Two defensible answers means the item cannot
discriminate a good router from a bad one. Replaced with *"What concerns have been raised about
the currently active proposals?"* — same boundary, one right answer.

Neither was fitting the test to the model: one label contradicted the router's own contract,
the other had no single correct answer.

---

## Decisions worth recording

### The router emits scope, not just route

The plan's three routes answer *which store*. Phase 4's measurements showed that is not enough:
a thematic question also needs a *width*, and getting it wrong regresses in both directions —
fan-out on a single-protocol question loses documents (4/4 → 3/4 on two eval questions), while
a flat search on a cross-protocol one misses documents no `k` recovers. `scope` is therefore
part of the routing contract and part of the eval set.

### Retrieval disables the relevance cutoffs; the judge decides

Phase 4 fitted `DEFAULT_MAX_DISTANCE` and `DEFAULT_MIN_GAP` on **unfiltered** searches. Applying
a protocol filter shrinks the candidate pool, makes results more homogeneous, and narrows the
gap for reasons unrelated to answerability — measured, per-protocol profiles shift enough to
flip verdicts. Rather than re-fit the constants per filter combination, the nodes retrieve
candidates with cutoffs off and the **relevance judge** decides whether anything answers the
question.

That is backed by measurement rather than preference: on the one pair no threshold can separate
(0.451 unanswerable versus 0.458 answerable — in the *wrong order*), judging scored 0 versus 3.
The same model **reranking for order** made thematic recall worse (0.78 → 0.72), so it judges
and does not reorder.

### The model picks templates, never writes SQL

Six parameterised templates; the model chooses a name and typed parameters. Every value is
NULL, a clamped integer, or a validated enum/protocol name. Injection attempts are *rejected*,
not escaped. Every template is asserted to constrain `is_current`, including a guard in
`render()` so a future template that forgets it fails loudly.

### Citations are derived from the prose, not declared beside it

A declared citation list can disagree with the text — markers cited but not declared, or
declared but never used. Reconciling the two is work with no upside when the prose is what a
reader sees. Markers are extracted from the answer and resolved against the passage map.

---

## Known limitations

**One routing miss remains (0.93).** `route-active-concerns` routes to `vector` instead of
`hybrid` — the router drops the "currently active" status constraint. Deliberately not chased:
the criterion is 0.80, and tuning a prompt against the 15th item of a 15-item set is exactly
the overfitting this project keeps catching.

**`is_current` cannot currently be demonstrated.** Silver holds **0 superseded rows** across
both tables — the corpus was harvested once and nothing has changed. The guard is protection
against a future re-harvest, not a presently-provable filter. The test **skips explicitly**
with that message rather than passing vacuously.

**The judge costs a call per query.** ~$0.0004 measured. Embeddings were a one-time corpus
cost; LLM calls are per question, so a retry loop is the first thing in this project that can
run up a bill. The provider spend cap stays essential.

**Multi-turn is possible but unused.** `add_messages` accumulates correctly (proven by test),
but `gov ask` is single-shot. Exposing follow-ups is a product decision, not a technical one.

---

## Tests

**64 new**, split so the free ones run anywhere.

| File | Marker | Needs |
| --- | --- | --- |
| `tests/test_phase5_graph.py` (41) | none | nothing |
| `tests/test_phase5_cli_snippet.py` (3) | none | nothing |
| `tests/integration/test_phase5_agent.py` (4) | `integration` | Postgres + Trino |
| `tests/integration/test_phase5_agent.py` (6) | `integration, live` | + API key |

The ones carrying weight:

- **`test_messages_actually_accumulate_through_the_graph`** — asserts behaviour, not just the
  annotation. Without the reducer, history is silently overwritten and every turn looks like
  the first.
- **`test_each_route_reaches_only_its_own_store`** — a "vector" question quietly running SQL
  is expensive and wrong, and nothing in the output would say so.
- **`test_a_fabricated_marker_is_dropped_and_reported`** — the citation contract, as a test.
- **`test_narrowing_keeps_forum_evidence`** — bug 5, pinned with the measured symptom in the
  docstring.
- **`test_injection_attempts_are_rejected_not_escaped`** — templates cannot be talked into
  `DROP TABLE`.
- **`test_scope_one_is_only_used_when_a_protocol_is_identifiable`** — enforces the eval-set
  rule that the first run violated, so the same labelling error cannot return.
- **`test_relative_dates_resolve_against_today_not_an_invented_year`** — bug 4.
- **`test_an_unanswerable_question_is_refused_rather_than_answered`** — the judge, end to end,
  on the case Phase 4 proved no threshold can reject.
- **`test_multiline_text_that_fits_is_not_marked_truncated`** — bug 7.

Also caught by an existing Phase 0 test: `ROUTER_MODEL` and `SYNTHESIS_MODEL` were read in code
but absent from `.env.example`. That guard has now paid for itself twice.

**And one it did not catch.** `langgraph` was installed by hand into the local venv and was
absent from `pyproject.toml` for the entire build — a fresh clone would have failed at import
with nothing in the repo explaining why. `pip install -e .` succeeding locally proves nothing
when the local venv already has the package. Fixed, verified against a throwaway virtualenv,
and pinned by a new guard (`test_every_third_party_import_is_a_declared_dependency`) so the
same class of omission fails the build rather than the next clone.

---

## What this says about method

Every one of the seven bugs was found by **running the thing and reading the output carefully**,
not by a test failing. The tests came after, to stop them returning. Two patterns recur:

**Provenance is where the interesting failures live.** Three of the seven (unresolvable
citations, deleted forum evidence, refused counting questions) were about *where a claim came
from* rather than whether the claim was right. Retrieval quality gets measured obsessively;
provenance quietly did not, until a validator was pointed at it.

**A guard that reports its own reasoning is worth more than one that silently succeeds.** The
narrowing bug was visible only because the node writes a `retrieval_note`; the citation bug only
because the validator names what failed to resolve. Both would otherwise have looked like
slightly thin answers.

---

## What's next

Phase 6 — the FastAPI delivery layer. Two carried items:

1. **`await agent_workflow.ainvoke(...)`**, not a sync `.invoke()` inside `async def`. The
   blueprint got this wrong, and one request blocking the event loop is invisible until load.
2. **Grow the routing eval set.** Fifteen questions found two of its own labelling errors; more
   questions, especially near the sql/hybrid boundary, is the cheapest available improvement in
   confidence.
