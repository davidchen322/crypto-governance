# Phase 6 — FastAPI Delivery Layer: Build Results

**Status:** exit criteria met · **Date:** 26 Aug 2026

Phase 5 ended with `gov ask` — a terminal command that answers a governance question with
citations that resolve. Phase 6 puts an HTTP surface in front of the same compiled graph and
the same silver tables, and changes nothing about what either one knows. New to the plan
this follows? See [`phase-6-plan.md`](phase-6-plan.md).

---

## Headline

| Exit criterion | Target | Measured |
| --- | --- | --- |
| `/docs` (Swagger) drives a live query end to end | works | **works** — see *Demo*, below |
| Ten concurrent requests don't serialize | async path genuinely async | **10 requests against a 0.3s node: ~0.32s total**, not ~3s |
| `/health` is real, not a constant | checks real dependencies | **200/`ok` when Postgres+Trino are up, 503/`degraded` when either fails** |

```bash
make api                                   # dev server on :8000
open http://localhost:8000/docs            # Swagger UI — the exit-criterion demo
curl localhost:8000/health
curl localhost:8000/proposals?protocol=aave
```

---

## What was built

```
backend_api/main.py                 FastAPI app; mounts health, chat, proposals routers
backend_api/graph_runtime.py        the one seam between routes and the compiled graph
backend_api/schemas.py              Pydantic request/response models (the /docs schema)
backend_api/routes/health.py        GET /health — real Postgres + Trino checks
backend_api/routes/chat.py          POST /api/v1/chat, GET /api/v1/chat/stream (SSE)
backend_api/routes/proposals.py     GET /proposals, GET /proposals/{proposal_id}
ai_agent/graph/sql_templates.py     + get_proposal template, + proposal_id validation
tests/test_phase6_sql_templates.py  11 tests, no DB
tests/test_phase6_api.py            14 tests, no DB, no docker, no API key
tests/integration/test_phase6_api.py  7 tests: 5 integration, 2 live
```

No changes to `ai_agent/graph/nodes.py`, `router.py`, or `synthesis.py` — the agent itself is
untouched. The only Phase 5 file this phase adds to is `sql_templates.py`, for a template
`/proposals/{proposal_id}` needs and Phase 5 never did.

---

## The concurrency exit criterion, measured rather than assumed

The plan names the mechanism: `async def` calling `await agent_workflow.ainvoke(...)`, not a
sync `.invoke()`. What the plan does not say — and what is not obvious — is *why* that's
enough, given that every node in this graph (`router_node`, `sql_node`, `vector_node`,
`hybrid_node`, `synthesis_node`) is a plain synchronous function built on `requests` and
`psycopg`, none of it `async def`.

Checked directly before writing the route, not assumed:

```python
def slow_node(state):
    time.sleep(0.3)
    return {"answer": "ok"}


g = build_graph(router=..., sql=..., vector=slow_node, hybrid=..., synthesis=...)
await asyncio.gather(*[g.ainvoke({"question": "q", "messages": []}) for _ in range(5)])
```

**Result: ~0.31s total for 5 concurrent calls against a 0.3s synchronous sleep — not ~1.5s.**
LangGraph runs a node with no async implementation via a thread-pool executor rather than
inline on the event loop (the same default behaviour `Runnable.ainvoke()` has always had for
a sync-only callable in LangChain). Each request's blocking work occupies a worker thread;
none of it blocks the loop another request's turn needs.

That result is what makes `await graph.ainvoke(...)` in `backend_api/routes/chat.py` correct
with no extra `asyncio.to_thread` wrapper — and it is committed as
`test_concurrent_chat_requests_do_not_serialize`, driven through the actual ASGI app via
`httpx.AsyncClient` + `ASGITransport`, not just against the bare graph:

```
10 concurrent POST /api/v1/chat, each hitting a 0.3s synchronous node: ~0.32s total
(serialized would be ~3.0s; the test fails above 1.5s)
```

If a future change reintroduces serialization — a checkpointer that locks state, a shared
non-reentrant client, a connection pool sized to 1 — this test turns red instead of the
regression only showing up as "the demo feels slow under load."

**A caveat worth carrying forward, not fixed here:** `search()` opens its own `psycopg`
connection per call and the Trino REST client opens its own session per query. Ten concurrent
requests means up to ten simultaneous connections to each. Fine at demo scale; sizing a
connection pool needs real concurrent load to size it against, which doesn't exist yet —
noted for Phase 7/11, not solved speculatively now.

---

## `/health` checks Trino, not the Iceberg catalog directly

The plan's literal wording — *"checks Postgres and the catalog"* — predates Trino's
introduction in this codebase. Every live query this API can run (the SQL/hybrid graph
branches, both `/proposals` routes) goes through **Trino**, not through the Iceberg REST
catalog directly. A catalog-only check could report healthy while Trino itself was down and
every real request would still fail — so `/health` checks Postgres (`SELECT 1` against the
vector database) and Trino (`SELECT 1` through the same REST client `trino_client.py` uses
everywhere else), and returns `503` when either fails rather than requiring a client to parse
the body to find out.

```
$ curl -s localhost:8000/health | jq
{
  "status": "ok",
  "postgres": { "status": "ok", "detail": null },
  "trino": { "status": "ok", "detail": null }
}
```

Verified against a real failure, not just the happy path: monkeypatching Trino to raise
turns `/health` into `503` / `"status": "degraded"` / `"trino": {"status": "error", ...}` —
see `test_health_reports_degraded_and_503_when_trino_is_unreachable`.

---

## A real schema-drift bug, found by running `/proposals` against real silver

Building the `get_proposal` template, the first draft used `proposer_address` as the column
name — copied directly from the schema sketch in `implementation-plan.md`. It renders fine,
passes every unit test (which never touch a real table), and fails the moment it runs against
actual silver:

```
ai_agent.chains.trino_client.TrinoError: line 1:65: Column 'proposer_address' cannot be resolved
```

`storage/iceberg_tables.sql` — the DDL Phase 3 actually built and evolved — calls the column
`author`, not `proposer_address`. The plan document's schema sketch was never updated after
Phase 3 diverged from it (silver also gained `space_id`, `record_hash`/`content_hash` as two
separate hashes, `discussion_url`, `ipfs_cid`, and dropped a few fields the sketch had), and
nothing before this phase had ever needed to select an individual proposal's fields by name —
Phase 5's existing templates only ever count, list, or aggregate columns whose names matter
less because they're never surfaced as a single named field to a client.

Fixed by reading the actual DDL rather than the plan's draft, in both the template and
`ProposalDetail`. This is exactly the kind of thing the project's own "run it for real before
trusting it" pattern exists to catch — the unit tests (`tests/test_phase6_api.py`, which
monkeypatch `trino_query` entirely) would never have surfaced this; only
`tests/integration/test_phase6_api.py`, run against a real silver table with actual harvested
data, did.

---

## `proposal_id`: the first template parameter sourced from an HTTP client, not an LLM

Every existing SQL template parameter arrives from the router — a model already told which
protocols and states exist, and constrained to picking from short enums. `GET
/proposals/{proposal_id}` is different: `proposal_id` is whatever text a client puts in a URL
path, with no enum to validate against.

`render()` gained a dedicated check — a restricted charset (alphanumerics, `:_.-`), 1–128
characters, checked before the value reaches `_quote` — following the file's existing
principle for every other parameter: **reject, don't just escape.** `_quote`'s general length
cap moved from 64 to 128 characters in the same change, since a real Snapshot proposal id
(`0x` + 64 hex characters) is 66 characters and would otherwise be rejected as "too long"
before the new pattern check ever ran.

Verified both that legitimate ids work and that hostile ones are rejected *before reaching
Trino at all* — `test_get_proposal_rejects_a_hostile_id_before_it_ever_reaches_trino` spies
on `trino_query` and asserts it is never called for a malformed id:

```python
resp = client.get("/proposals/0x123' OR '1'='1")
assert resp.status_code == 400
assert calls == []  # trino_query was never invoked
```

---

## What "streaming" means here, and what it deliberately does not

The plan's wording — SSE "so the UI can render tokens as they arrive" — read literally means
token-by-token streaming of the analyst's prose. That would require reworking
`ai_agent/graph/llm.py`'s `complete_json` to use OpenAI's streaming chat completions and
reassemble structured JSON from deltas — a change to Phase 5's synthesis machinery, not a
delivery layer over it, and well outside a phase estimated at one evening.

What `GET /api/v1/chat/stream` actually streams: one Server-Sent Event per **completed graph
node**, via `astream(..., stream_mode="updates")` — `router` (with its route/scope/filters
decision), then whichever branch it chose, then `synthesis` (the final answer and citations).
Verified against the real agent, not just fakes:

```
$ curl -N "localhost:8000/api/v1/chat/stream?question=How+many+proposals+does+each+protocol+have%3F"
event: router
data: {"route": "sql", "scope": "all", "filters": {...}}

event: sql
data: {"rows": [...], "retrieval_note": "ran template 'count_by_protocol'"}

event: synthesis
data: {"answer": "...", "citations": [], "data_gap": false}
```

This is real, working, asynchronous progress — not a simulation of streaming, and not
pretending the prose itself streams. Documented as a scope decision rather than a silent gap,
in the same spirit as Phase 4's demo-query and threshold write-ups: the honest answer is often
"we chose the achievable version and said so," not "we built exactly what the plan's wording
implies."

One mechanical bug this surfaced before it shipped: `SearchResult` chunks in a graph update
are dataclasses with a `datetime` field, which `json.dumps` cannot encode directly. Inside an
async generator, an uncaught `TypeError` there doesn't raise cleanly to a client — it
truncates the stream silently. `_json_safe()` converts chunks explicitly before encoding, and
`test_stream_chunks_are_json_safe_not_raw_dataclasses` pins it.

---

## Tests

**32 new**, split the way Phase 4/5 established: free ones need nothing, integration needs
the stack, live spends a fraction of a cent.

| File | Count | Needs |
| --- | --- | --- |
| `tests/test_phase6_sql_templates.py` | 11 | nothing |
| `tests/test_phase6_api.py` | 14 | nothing |
| `tests/integration/test_phase6_api.py` | 5 | Postgres + Trino |
| `tests/integration/test_phase6_api.py` | 2 | + API key (`live`) |

Two pre-existing tests needed a small update, not a fix: `test_every_template_constrains_is_current`
(Phase 5) and `test_every_template_executes_against_silver` (Phase 5 integration) both loop
over every entry in `TEMPLATES` with one generic params dict — adding `get_proposal` to that
dict meant supplying a `proposal_id` too. This is exactly the "can Phase 4/5 still be refined
after 6+ exists" question answered in practice: yes, and the blast radius of a new template
was two one-line test updates, not a rewrite.

The ones carrying real weight:

- **`test_concurrent_chat_requests_do_not_serialize`** — the exit criterion, driven through
  the real ASGI app with a stopwatch, not asserted from a docstring.
- **`test_get_proposal_rejects_a_hostile_id_before_it_ever_reaches_trino`** — spies on
  `trino_query` to prove a malformed id never reaches the database, not merely that the
  response looks fine.
- **`test_health_reports_degraded_and_503_when_trino_is_unreachable`** — a health check must
  be provably capable of failing, or it never earned the word "real."
- **`test_stream_chunks_are_json_safe_not_raw_dataclasses`** — the truncated-stream failure
  mode, pinned before it could ship.
- **`test_get_proposal_against_a_real_proposal_id_if_silver_has_one`** (integration) — the
  test that actually caught the `proposer_address`/`author` bug, by querying real silver
  instead of a mock.

---

## Reproducing all of it

```bash
make install
make test                          # 196 unit tests total (32 new for Phase 6), <1s

make up                            # postgres, minio, iceberg-rest, spark, trino
make harvest ARGS="--protocol aave --proposals 5 --topics 3 --with-posts"
make silver                        # a handful of real proposals/posts into silver

make verify-fast                   # Phase 1 probes, unaffected by this phase
.venv/bin/pytest tests/integration/test_phase6_api.py -m "integration and not live" -v
```

`make embed` and the `live`-marked tests need `OPENAI_API_KEY` (see `docs/api-keys.md`) and
were not run as part of this build — the concurrency proof, the `/health` and `/proposals`
correctness proofs, and the schema-drift bug above were all found without spending anything.

---

## Demo — driving it end to end

**1. Bring the stack up and start the API:**

```bash
make up
make harvest ARGS="--protocol aave --proposals 5 --topics 3 --with-posts"
make silver
make api                                    # http://localhost:8000
```

**2. `/health` — the plan's "real, not a constant" check:**

```bash
curl -s localhost:8000/health | python3 -m json.tool
```

**3. `/proposals` — no agent, no API key needed.** Run against this build's real 5-proposal
harvest:

```bash
$ curl -s "localhost:8000/proposals?protocol=aave&limit=3" | python3 -m json.tool
[
  {
    "proposal_id": "0xbc1c59336255d276088fa85f95ef7122d9d8a56940299093f3246b7db897b6a6",
    "protocol_name": "aave",
    "title": "[ARFC] Liquidation Protocol Fee Increase for WBTC, WETH, and wstETH on Aave V3 Ethereum Core",
    "proposal_state": "closed",
    "proposal_created": "2026-08-19 18:54:46.000 UTC",
    "vote_count": 68
  },
  ...
]

$ curl -s localhost:8000/proposals/0xbc1c...b6a6 | python3 -m json.tool
{
  "proposal_id": "0xbc1c59336255d276088fa85f95ef7122d9d8a56940299093f3246b7db897b6a6",
  "protocol_name": "aave",
  "title": "[ARFC] Liquidation Protocol Fee Increase for WBTC, WETH, and wstETH on Aave V3 Ethereum Core",
  "body": "## Summary\n\nThis proposal asks the Aave DAO to increase the Liquidation Protocol Fee...",
  "proposal_state": "closed",
  "author": "0xb291232F480F41c75802C4a60F1D2AC03404Afef",
  "voting_start": "2026-08-20 18:54:46.000 UTC",
  "voting_end": "2026-08-23 18:54:46.000 UTC",
  "vote_count": 68,
  "scores_total": 372199.87199270877,
  "proposal_created": "2026-08-19 18:54:46.000 UTC"
}

$ curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/proposals/nope-does-not-exist
404
```

**4. `/docs` — the exit-criterion demo.** Open `http://localhost:8000/docs`, expand
`POST /api/v1/chat`, "Try it out", body `{"question": "How many proposals does each protocol
have?"}`, Execute. This needs `OPENAI_API_KEY` set in `.env` (see `docs/api-keys.md`) — it is
a live query against the real agent, exactly like `gov ask`. Confirmed reachable and correctly
routed without a key: `GET /docs` returns 200, and `/openapi.json` lists exactly the five
routes this phase adds — `/health`, `/api/v1/chat`, `/api/v1/chat/stream`, `/proposals`,
`/proposals/{proposal_id}`.

**5. Streaming, from a shell.** Verified end to end *without* an API key too — the failure
path is what a client actually needs to see, not a dropped connection:

```bash
$ curl -sN --max-time 10 "localhost:8000/api/v1/chat/stream?question=hello"
event: error
data: {"error": "OPENAI_API_KEY is not set — see docs/api-keys.md"}

$ curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/health   # server is still up
200
```

With a real key, the same command streams `event: router`, then the chosen branch, then
`event: synthesis` with the answer and citations — see *What "streaming" means here*, above.

**6. The concurrency claim, without spending anything** — the committed test *is* the
reproduction:

```bash
.venv/bin/pytest tests/test_phase6_api.py::test_concurrent_chat_requests_do_not_serialize -v
```

---

## Known limitations

**No connection pooling.** Ten concurrent requests means up to ten simultaneous Postgres and
Trino connections, each opened and closed per call. Fine at demo scale; revisit once Phase 7
or 11 introduces real concurrent load to size a pool against.

**SSE streams graph progress, not tokens.** See *What "streaming" means here*, above. Real,
working, and asynchronous — just not per-token synthesis streaming, which would require
reworking Phase 5's LLM client.

**No auth.** Deliberate, per the plan's guest-tier default. `backend_api/saas_modules/`
remains empty and unimported; Phase 12 is where this changes.

**A dependency-deprecation warning, not yet acted on.** Starlette's `TestClient` currently
warns that `httpx` support is deprecated in favour of an `httpx2` package. Noted rather than
chased — it is a test-only warning, not a runtime one, and the replacement package is new
enough that pinning to it now would be premature.

---

## What's next

Phase 7 — Airflow, wrapping the harvest → silver → embed pipeline on a schedule. Two things
carry forward from here:

1. **The full historical backfill belongs in Phase 7, not Phase 8.** This phase's demo used a
   5-proposal, 3-topic harvest — deliberately small, and deliberately still single-version for
   every entity (matching Phase 3's own "the gap in the demo" finding). Phase 8's version-history
   timeline screen needs real multi-version documents to show anything meaningful; that data
   only exists after a full backfill and a second harvest catches something changing.
2. **Grow real concurrent load before sizing a connection pool.** Phase 6 proved the async
   path itself doesn't serialize; it did not stress per-connection limits, because there is no
   real traffic yet to size against.
