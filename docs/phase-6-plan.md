# Phase 6 — FastAPI Delivery Layer

**Status:** planned, not started · **Written:** 26 Aug 2026 · **Estimate:** ~1 evening

Phase 5 ended with a working agent: `gov ask "…"` answers a governance question with
citations that resolve. Phase 6 does not change what the agent knows or how it answers — it
puts an HTTP surface in front of it, so a browser or a dashboard can reach the same thing the
terminal already reaches.

---

## What Phase 6 actually is

Three things, over what already exists:

1. **`POST /api/v1/chat`** — one question in, one analyst answer out. The HTTP twin of
   `gov ask`.
2. **`GET /api/v1/chat/stream`** — the same question, delivered as Server-Sent Events, so a
   UI can show progress instead of a blank wait during the seconds a synthesis call takes.
3. **`GET /proposals`, `GET /proposals/{proposal_id}`** — list and detail views over silver,
   for a UI screen that does not need the agent at all (Phase 8's proposal list and detail
   screens read these directly).

Plus a fourth thing the plan calls out specifically: **`/health` has to be real.** Not
`{"status": "ok"}` — a check that a load balancer, an uptime monitor, or a person can trust.

Nothing here re-implements routing, retrieval, the relevance judge, or citation validation.
Every route is a thin layer over work Phases 3–5 already did and already measured. If an
answer looks wrong through the API, the agent is wrong; this layer only delivers it.

---

## Why two chat endpoints, not one

The plan's wording is `/api/v1/chat` using `ainvoke`, with SSE streaming "so the UI can
render tokens as they arrive." Read literally, that is token-by-token streaming of the
analyst's prose — which is not what this phase builds, and the reason is worth stating before
writing any code rather than discovering it midway through.

`ai_agent/graph/llm.py`'s `complete_json` makes one request/response call per node — it asks
for `response_format: json_object` and gets the whole JSON object back at once. Making the
*prose itself* stream token-by-token means reworking that client to use OpenAI's streaming
chat completions and reassembling structured output from deltas. That is a change to Phase
5's synthesis machinery, not a delivery layer sitting on top of it, and it is out of scope for
a phase the plan itself estimates at one evening.

What genuinely is available for free: LangGraph's compiled graph supports `astream(...,
stream_mode="updates")`, which yields one update per **node** as it completes — router,
then whichever branch it chose, then synthesis. That is real, working, asynchronous
progress ("routing…", "retrieving…", "answering…"), not a simulation of streaming. It is the
honest middle ground between "no feedback until the whole answer is ready" and a
rewrite of Phase 5's LLM client.

So:

- `POST /api/v1/chat` — the synchronous route. One JSON response, matching `gov ask --json`'s
  shape. This is also the route Swagger's "Try it out" can actually drive — SSE does not
  render usefully in the auto-generated docs UI for a POST body.
- `GET /api/v1/chat/stream?question=…` — SSE, one event per completed graph node. A GET with
  a query string is what `curl` and a browser `EventSource` can hit directly without a
  request body.

---

## The concurrency requirement, and why it needs a stopwatch, not a docstring

The plan's exit criterion: **ten concurrent requests don't serialize — the async path is
genuinely async.** The corrections table already names the mechanism: `async def` calling
`await agent_workflow.ainvoke(...)`, not a sync `.invoke()` inside an async function.

That correction is necessary but its sufficiency is not obvious, and it is worth checking
before building on top of it rather than after. Every node in this graph — router, sql,
vector, hybrid, synthesis — is a **plain synchronous function**. `router_node` calls
`requests` under the hood via `ai_agent/graph/llm.py`; `vector_node` opens a `psycopg`
connection. None of them are `async def`. So the real question is: when a *sync* node hangs
off an `await graph.ainvoke(...)` call, does the event loop actually stay free for a second
request, or does `ainvoke` just call the sync function inline and block anyway?

This was measured directly before committing to the design, not assumed:

```python
def slow_node(state):
    time.sleep(0.3)
    return {"answer": "ok"}


g = build_graph(router=..., sql=..., vector=slow_node, hybrid=..., synthesis=...)

# 5 concurrent ainvoke() calls, each hitting a 0.3s synchronous sleep
await asyncio.gather(*[g.ainvoke({"question": "q", "messages": []}) for _ in range(5)])
```

Result: **~0.31s total, not ~1.5s.** LangGraph runs a node with no async implementation via a
thread-pool executor rather than inline on the event loop — the same default behaviour
LangChain's `Runnable.ainvoke()` has always had for a sync-only callable. Five requests each
occupy a worker thread; none of them block the loop the sixth request would need.

This is why the FastAPI route can be exactly `await graph.ainvoke(...)` with no additional
`asyncio.to_thread` wrapper at the API layer — the plan's stated fix is sufficient, measured
rather than assumed, and the experiment above is committed as
`test_concurrent_chat_requests_do_not_serialize` so a future change that breaks this (for
instance, swapping in a checkpointer that serializes access, or a node that acquires a
non-reentrant lock) fails a test instead of only showing up as a slow demo under load.

One caveat worth naming before it is discovered the hard way: `ai_agent/chains/retrieval.py`'s
`search()` opens and closes its own `psycopg` connection per call, and Trino's REST client
does the same per query. Ten concurrent requests means up to ten simultaneous Postgres
connections and ten simultaneous Trino sessions. Fine at demo scale; a connection pool is a
Phase 7/11 concern once there is real concurrent load to size against, not something to
add speculatively here.

---

## `/proposals` reuses the SQL templates, not a second query surface

`ai_agent/graph/sql_templates.py` already has `list_proposals` (protocol, state, since,
limit — all validated, `is_current` enforced). The list endpoint calls it directly rather
than writing new Trino SQL in `backend_api/`. That leaves exactly one place that knows how
to query `proposal_versions` safely.

The detail endpoint (`GET /proposals/{proposal_id}`) needs a new template, because nothing in
Phase 5 ever looked up one proposal by id — the SQL node only ever counted, listed or
filtered. Adding `get_proposal` surfaces something Phase 5's threat model did not need to
consider: every existing template parameter arrives from the *router*, an LLM already told
which protocols and states exist and constrained to picking from short enums. `proposal_id`
on this new template arrives from an **HTTP path segment** — arbitrary client-supplied text,
the first template parameter that does.

`render()`'s existing parameters lean on enum validation (protocol, state) or an integer cast
(limit) for safety; none of that machinery fits a proposal id, which is an open-ended string.
So `proposal_id` gets its own pattern check — a restricted charset, no quotes, no whitespace,
no SQL punctuation — checked before the value ever reaches the query, in keeping with the
file's existing principle: **injection attempts are rejected, not escaped.** `_quote`'s
general length cap also needs raising from 64 to 128 characters, since a real Snapshot
proposal id (`0x` + 64 hex characters) is 66 characters long and would otherwise be rejected
as "too long" before the new pattern check ever runs.

---

## `/health` checks Trino, not the raw Iceberg catalog

The plan's wording: *"a real `/health` that checks Postgres and the catalog rather than
returning a constant."* Read against Phase 1, "the catalog" means the Iceberg REST service
directly (`GET /v1/config`, the same endpoint Phase 1's infrastructure probe hits).

That wording predates Trino's introduction. Since Phase 5, every live query this API can run
— the SQL and hybrid graph branches, and both `/proposals` routes — goes through **Trino**,
not through the Iceberg REST catalog directly. A health check against the raw catalog could
report healthy while Trino itself is down, degraded, or misconfigured, and every real request
this service serves would still fail. Checking Trino (`SELECT 1`, not a port probe) is what
actually predicts whether a live request succeeds — the same reasoning Phase 1's own
corrected healthchecks used to reject "TCP port open" as a stand-in for "the service works."

`/health` therefore checks Postgres (`SELECT 1` against the vector database) and Trino, and
returns `200` only when both succeed — `503` otherwise, so the status code itself is
actionable rather than requiring a client to parse the body to find out.

---

## No auth, on purpose

The plan: *"Keep the blueprint's guest-tier fallback so the demo needs no key."* Phase 6 adds
no authentication anywhere. `backend_api/saas_modules/` stays empty and unimported, exactly as
it has been since Phase 0 — multi-tenant auth is Phase 12, deliberately deferred, and nothing
in this phase should make that migration harder than it already will be.

---

## Build order

1. **`get_proposal` template + `proposal_id` validation** in `ai_agent/graph/sql_templates.py`
   — the one piece of new Phase 5 surface this phase needs, done first so `/proposals`
   detail has something safe to call.
2. **`backend_api/graph_runtime.py`** — a single seam (`get_graph()`) between routes and the
   compiled graph, so tests can swap in a fake graph the same way `tests/test_phase5_graph.py`
   already does, without monkeypatching langgraph internals.
3. **`/health`** — real checks first, before anything that depends on the stack being up is
   built, so a broken environment fails loudly from the first route rather than the last.
4. **`/proposals` list and detail** — no agent involved, so this can be tested independent of
   the chat routes.
5. **`POST /api/v1/chat`** — `await graph.ainvoke(...)`, shaped like `gov ask --json`.
6. **`GET /api/v1/chat/stream`** — `astream(..., stream_mode="updates")`, one SSE event per
   node, with a JSON-safety pass over `chunks` (dataclasses, not dicts) before encoding.
7. **The concurrency test** — proven with a synthetic slow node before trusting the demo to
   show it under real network latency.
8. **`make api`** — the dev-server target, and confirm `/docs` actually drives a live query
   end to end.

---

## How we will know it works

**Exit criteria, from the plan:**

- The auto-generated Swagger page at `/docs` drives a live query end to end.
- Ten concurrent requests don't serialize — the async path is genuinely async.

**Tests, in the Phase 4/5 style — a passing status code is not evidence on its own:**

| Test | Guards |
| --- | --- |
| `/health` returns 503 and `status: degraded` when a dependency fails | a health check that can only ever report healthy |
| `proposal_id` containing SQL punctuation is rejected before reaching Trino | the first template parameter sourced from an HTTP client rather than an LLM |
| `chat` reports `data_gap` rather than inventing an answer | the API silently discarding what the relevance judge already decided |
| the SSE stream emits `router` → branch → `synthesis`, in order | a stream that silently drops or reorders progress events |
| chunks in an SSE event are JSON-safe (not raw dataclasses) | a `TypeError` inside an async generator, which surfaces as a silently truncated stream |
| N concurrent `POST /api/v1/chat` calls against a slow node complete in ~1x, not ~Nx, the delay | the exact exit criterion, measured with a stopwatch rather than asserted from a docstring |

---

## What this phase does *not* do

No dashboard (Phase 8), no scheduling (Phase 7), no contract source (Phase 9), no auth
(Phase 12), no connection pooling (real load hasn't been measured yet to size one against),
and no token-by-token streaming of the analyst's prose (would require reworking Phase 5's
LLM client — a defensible future change, not a silent gap in this one). Phase 6 ends at an
HTTP surface that answers exactly what `gov ask` and `gov search` already answer, reachable
by anything that can speak HTTP.
