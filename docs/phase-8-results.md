# Phase 8 — Next.js Dashboard: Build Results

**Status:** exit criteria met · **Date:** 9 Sep 2026

Phase 7 gave the platform a real, full-history, multi-protocol corpus. Phase 8 puts a
browser in front of it — four screens, reading `backend_api` directly, with no mocked data
anywhere. Plan this follows: [`phase-8-plan.md`](phase-8-plan.md).

---

## Headline

| Exit criterion | Target | Measured |
| --- | --- | --- |
| No mocked data anywhere | every screen traces to a real API call | **confirmed live in a browser**, not just build/lint/type-check |
| Full corpus reachable, not just first 100 | `offset` pagination reaches all 978 Aave proposals | **works** — a real Trino bug (`OFFSET`/`LIMIT` order) was caught and fixed building this |
| Risk panel flags are evidence-backed | a flagged and a clean real proposal, numbers checkable | **confirmed** — real proposal renders "no flags raised" correctly |
| Version timeline shows real SCD2 history | the known 6-version Aave proposal renders correctly | **confirmed** — all 6 versions, `pending → active`, vote_count 0→8→12→19→51→53 |
| Citations always resolve | no dead marker in any rendered answer | **confirmed, including the edge case**: a citation that failed backend validation renders as plain non-clickable text, not a broken link |

```bash
make api                                   # dev server on :8000 (needed first)
make dashboard                             # Next.js dev server on :3000
open http://localhost:3000/proposals
```

---

## What was built

```
backend_api/main.py                       + CORS middleware (DASHBOARD_ORIGINS)
backend_api/schemas.py                    + ProposalVersion; ProposalDetail widened
backend_api/routes/proposals.py           + GET /proposals/{id}/history; list_proposals offset
ai_agent/graph/sql_templates.py           + get_proposal_history; list_proposals offset;
                                             get_proposal widened (quorum/choices/scores/discussion_url)
tests/test_phase6_sql_templates.py        + 6 tests for the above
.env.example                              + DASHBOARD_ORIGINS

dashboard/                                 new Next.js 16 app (App Router, Tailwind, TS)
  lib/api.ts                                typed fetch client for every backend_api route
  lib/risk.ts                               the risk panel's three heuristics, defined concretely
  app/proposals/page.tsx                    screen 1 — list, filters, pagination
  app/proposals/[id]/page.tsx               screen 2 — detail + risk panel
  app/proposals/[id]/history/page.tsx       screen 4 — version-history timeline
  app/chat/page.tsx                         screen 3 — SSE streaming chat + citations
```

The backend additions came first and were verified with `curl` before any frontend code
existed — the plan's own build order, followed in practice, not just on paper.

---

## Three gaps the blueprint didn't mention, closed before any UI code

The original blueprint's Phase 8 line — *"proposal detail with the risk panel... a
version-history timeline"* — assumed things that, once checked against the actual code,
didn't exist yet:

1. **No endpoint ever returned a proposal's non-current versions.** Every existing route
   filtered `WHERE is_current`. Added `get_proposal_history` (deliberately *not*
   current-only — see the comment in `sql_templates.py` explaining why this template
   still passes the module's own "did you forget `is_current`" guard) and
   `GET /proposals/{id}/history`.
2. **"Risk panel" had no backing signal anywhere in Phases 0–7** (confirmed by grepping the
   whole repo before writing the plan). Defined now as three explainable heuristics —
   quorum not met, close vote, low turnout — computed client-side in `lib/risk.ts` from
   columns silver already had. Every flag renders with the real numbers that triggered it.
3. **`get_proposal` didn't select the columns the risk panel needs.** `quorum`, `choices`,
   `scores`, `discussion_url` are all real silver columns; none were in the original
   `SELECT` or `ProposalDetail`. Widened both.

---

## A real bug found building the pagination, not designing it

`list_proposals`'s template originally read `ORDER BY proposal_created DESC LIMIT {limit}`.
Adding `OFFSET {offset}` after `LIMIT` — the Postgres/MySQL order — failed immediately
against the live API:

```
ai_agent.chains.trino_client.TrinoError: line 1:344: mismatched input 'OFFSET'. Expecting: <EOF>
```

Trino requires `OFFSET` *before* `LIMIT`. One-line fix (`ORDER BY ... OFFSET {offset} LIMIT
{limit}`), caught immediately because the plan's own discipline was followed: curl the real
endpoint before writing frontend code that depends on it, rather than trust that a clean
`render()` unit test (which doesn't touch a real database) meant the SQL was actually valid.

---

## The version-history screen — verified against real data, not asserted

This is the screen the whole phase exists to prove out, so it got the most scrutiny. Before
writing any UI, the corpus was queried directly to confirm it actually had something real to
show:

```
proposals with real version history: 3
0xe7ce2a6c... aave 6 versions   <- used as the verification target throughout
```

Live in a browser, clicking through to that exact proposal's history renders all 6 versions,
in order, with the state transition and vote counts climbing correctly at every step —
screenshotted and checked by eye against the same data queried directly from Trino, not
inferred from the API response alone.

---

## Live browser verification, not just a green build

`npm run build` passing (including full TypeScript checking) was treated as necessary, not
sufficient. Every screen was exercised in a real browser against the live stack:

- **List**: real titles/protocols/states/vote counts, pagination advancing to genuinely
  different proposals at `offset=0` vs `offset=1`
- **Detail**: the known clean proposal correctly shows "No risk flags raised" rather than an
  empty or missing panel
- **History**: the known 6-version proposal, as above
- **Chat**: real SSE stage progress (`"Routing..."` → `"Retrieving and querying..."`) visibly
  ahead of the final answer, a real synthesized answer with a citation, and clicking that
  citation navigating to the exact right proposal
- **A genuine edge case, not a bug**: asking an unanswerable-in-spirit question
  (`"What is the current price of UNI?"`) got a real, hedged answer citing one tangentially
  related chunk — and that citation's marker had failed the backend's own validation, so it
  rendered as plain, non-clickable text rather than a link. This is `synthesis.py`'s
  documented "a fabricated citation fails a lookup instead of reaching a reader" holding true
  through the UI, not a rendering bug.

One real, current-tooling issue surfaced along the way: `eslint-plugin-react-hooks`'s newer
`set-state-in-effect` rule flagged calling `setState` synchronously inside a data-fetching
effect. Fixed by moving those calls into the event handlers that trigger each fetch (a filter
changing, "Load more" being clicked) — every trigger for a new request here already is a
user action, so the handler is the right place for it, and the effect itself only ever sets
state inside its async callbacks.

---

## A CI failure this phase caused, and the fix

Pushing this phase's commit broke CI — both jobs failed on the same assertion:

```
FAILED tests/test_phase0_repo.py::test_every_env_var_used_in_code_is_documented
AssertionError: env vars read in code but absent from .env.example: {'DASHBOARD_ORIGINS': 'backend_api/main.py'}
```

`DASHBOARD_ORIGINS` (the CORS middleware's env var) was documented in `dashboard/.env.example`
for the frontend's own settings, but never added to the *root* `.env.example` that Phase 0's
own hygiene test checks against. One-line fix, confirmed locally before repushing.

---

## Tests

**6 new**, all free — no database needed, matching how `get_proposal`'s own `proposal_id`
validation was originally tested.

| File | Count | Needs |
| --- | --- | --- |
| `tests/test_phase6_sql_templates.py` | 6 | nothing |

- **`test_get_proposal_history_renders_without_an_is_current_filter`** — proves the new
  template deliberately omits the current-only filter while still satisfying `render()`'s
  guard, for the reason documented in the template itself.
- **`test_list_proposals_offset_advances_the_page`** / **`test_list_proposals_rejects_a_negative_offset_by_clamping_to_zero`**
  — the pagination contract, pinned before the frontend was built against it.

No new frontend tests — the dashboard was verified live against the real stack instead (see
above), which is what actually exercises it end to end; a component test suite would mostly
duplicate that against a mocked API.

---

## Reproducing all of it

```bash
make install
make test                          # 210 unit tests total (6 new for Phase 8), <3s

make up                            # postgres, minio, iceberg-rest, spark, trino
make api                           # FastAPI on :8000

cd dashboard && npm install
cd .. && make dashboard             # Next.js on :3000
open http://localhost:3000/proposals
```

The dashboard needs real data in silver to show anything meaningful — `make harvest` /
`make silver` / `make embed` per earlier phases' instructions, or the real full-history
corpus from Phase 7's backfill if that's already been run.

---

## Known limitations

- **No forum-topic detail screen.** Citations to forum content render as an inline
  expandable snippet, not a navigable page — named explicitly as out of scope in the plan.
- **Risk panel thresholds are hand-picked constants** (5% close-vote margin, 10-vote turnout
  floor), not derived from each protocol's own historical distribution. Documented as
  tunable in `lib/risk.ts`, same discipline `DEFAULT_MAX_DISTANCE` already models.
- **No real-time updates.** A proposal's data is exactly as fresh as the last DAG run.
- **No authentication** — unchanged from Phase 6's own stance; real auth is Phase 12.
- **No deployment.** This runs against the local API; a public URL is Phase 11.

## What's next

Phase 9 (contract source ingestion) and Phase 10 (alerting) are the next phases that could
give the risk panel a second, more substantive signal beyond the three heuristics defined
here. Phase 11 (cloud deployment) is what would put a public URL on this dashboard.
