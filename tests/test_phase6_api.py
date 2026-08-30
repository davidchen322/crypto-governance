"""Phase 6 acceptance: the FastAPI delivery layer.

No database, no API key, no docker — every dependency the routes would normally reach
(the compiled graph, Trino, Postgres) is replaced with a fake or a monkeypatch. What these
tests cover is the plan's own exit criteria plus the failure shapes that would otherwise ship
silently, in the style Phases 4 and 5 established: a passing status code is not evidence,
a fabricated citation reaching a client is not caught by "the request succeeded", and a
concurrency claim gets a stopwatch, not an assertion on a docstring.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi.testclient import TestClient

from ai_agent.graph.workflow import build_graph
from backend_api import graph_runtime
from backend_api.main import app
from backend_api.routes import chat, health, proposals

client = TestClient(app)


def fake_graph(**overrides):
    """A compiled graph with canned nodes, built the same way test_phase5_graph.py does —
    the topology is real; only the node bodies are fakes."""
    nodes = {
        "router": lambda s: {"route": "vector", "scope": "one", "filters": {"protocol": "aave"}},
        "sql": lambda s: {},
        "vector": lambda s: {"chunks": [], "retrieval_note": "searched aave with k=20"},
        "hybrid": lambda s: {},
        "synthesis": lambda s: {
            "answer": "Aave did X [c0].",
            "citations": [
                {
                    "marker": "c0",
                    "chunk_ref": "proposal:0xabc#1",
                    "source": "proposal",
                    "document_id": "0xabc",
                    "protocol_name": "aave",
                    "title": "[ARFC] Something",
                    "distance": 0.31,
                }
            ],
            "data_gap": False,
        },
    }
    nodes.update(overrides)
    return build_graph(
        router=nodes["router"],
        sql=nodes["sql"],
        vector=nodes["vector"],
        hybrid=nodes["hybrid"],
        synthesis=nodes["synthesis"],
    )


# --------------------------------------------------------------------------
# /health — must reflect real dependency state, not a constant
# --------------------------------------------------------------------------


def test_health_reports_ok_when_every_dependency_is_reachable(monkeypatch):
    monkeypatch.setattr(health, "_check_postgres", lambda: health.HealthComponent(status="ok"))
    monkeypatch.setattr(health, "_check_trino", lambda: health.HealthComponent(status="ok"))

    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["postgres"]["status"] == "ok"
    assert body["trino"]["status"] == "ok"


def test_health_reports_degraded_and_503_when_trino_is_unreachable(monkeypatch):
    """The plan's own wording: a real health check, not a constant. `/health` must be able
    to fail, and it must fail with a status code a load balancer can act on."""
    monkeypatch.setattr(health, "_check_postgres", lambda: health.HealthComponent(status="ok"))
    monkeypatch.setattr(
        health, "_check_trino", lambda: health.HealthComponent(status="error", detail="timeout")
    )

    resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["trino"]["status"] == "error"
    assert body["postgres"]["status"] == "ok"


# --------------------------------------------------------------------------
# POST /api/v1/chat — the synchronous, Swagger-friendly route
# --------------------------------------------------------------------------


def test_chat_returns_the_agents_answer_with_resolvable_citations(monkeypatch):
    monkeypatch.setattr(chat, "get_graph", fake_graph)

    resp = client.post("/api/v1/chat", json={"question": "What is Aave doing about risk?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["question"] == "What is Aave doing about risk?"
    assert body["route"] == "vector"
    assert body["scope"] == "one"
    assert body["data_gap"] is False
    assert body["answer"] == "Aave did X [c0]."
    assert body["citations"][0]["marker"] == "c0"
    assert body["citations"][0]["document_id"] == "0xabc"


def test_chat_reports_a_data_gap_rather_than_inventing_an_answer(monkeypatch):
    graph = fake_graph(
        synthesis=lambda s: {
            "answer": "DATA GAP IDENTIFIED — the corpus does not contain material that "
            "answers this question.",
            "citations": [],
            "data_gap": True,
        }
    )
    monkeypatch.setattr(chat, "get_graph", lambda: graph)

    resp = client.post("/api/v1/chat", json={"question": "How much is the Aave founder paid?"})

    body = resp.json()
    assert body["data_gap"] is True
    assert body["citations"] == []


def test_chat_rejects_a_request_with_no_question():
    resp = client.post("/api/v1/chat", json={})
    assert resp.status_code == 422, "a missing required field should fail validation, not crash"


# --------------------------------------------------------------------------
# GET /api/v1/chat/stream — one SSE event per completed graph node
# --------------------------------------------------------------------------


def test_stream_emits_one_event_per_completed_node_in_order(monkeypatch):
    monkeypatch.setattr(chat, "get_graph", fake_graph)

    with client.stream(
        "GET", "/api/v1/chat/stream", params={"question": "What is Aave doing about risk?"}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = [line for line in resp.iter_lines() if line.startswith("event: ")]

    # router -> (its chosen branch) -> synthesis, in that order, matching build_graph's
    # topology in ai_agent/graph/workflow.py.
    assert events == ["event: router", "event: vector", "event: synthesis"]


def test_stream_chunks_are_json_safe_not_raw_dataclasses(monkeypatch):
    """SearchResult is a dataclass with a datetime field; naively json-encoding a graph
    update containing `chunks` would raise inside the generator, which — for a streaming
    response — surfaces as a silently truncated stream, not a clean error."""
    from datetime import UTC, datetime

    from ai_agent.chains.retrieval import SearchResult

    hit = SearchResult(
        source="proposal",
        document_id="0xabc",
        protocol_name="aave",
        chunk_index=0,
        heading=None,
        text="some text",
        distance=0.31,
        title="[ARFC] Something",
        document_date=datetime(2026, 8, 11, tzinfo=UTC),
    )
    graph = fake_graph(vector=lambda s: {"chunks": [hit], "retrieval_note": "searched aave"})
    monkeypatch.setattr(chat, "get_graph", lambda: graph)

    with client.stream("GET", "/api/v1/chat/stream", params={"question": "q"}) as resp:
        body = "".join(resp.iter_text())

    assert '"document_date": "2026-08-11T00:00:00+00:00"' in body
    assert "distance" in body and "0.31" in body


# --------------------------------------------------------------------------
# The Phase 6 exit criterion: ten concurrent requests do not serialize
# --------------------------------------------------------------------------


def test_concurrent_chat_requests_do_not_serialize(monkeypatch):
    """The plan's exit criterion, verbatim: 'ten concurrent requests don't serialize — the
    async path is genuinely async.' A synchronous stand-in for a slow node (network calls in
    the real nodes behave the same way — they release the GIL while waiting on I/O) proves
    it: N concurrent requests against a node that blocks for DELAY seconds should complete in
    close to DELAY seconds total, not N x DELAY. If a future change swaps `ainvoke` for a
    sync `.invoke()` inside this route, this test goes from ~0.3s to ~3s and fails loudly
    rather than only showing up as a slow demo under load.
    """
    DELAY = 0.3
    N = 10

    def slow_node(state: dict[str, Any]) -> dict[str, Any]:
        time.sleep(DELAY)
        return {"chunks": [], "retrieval_note": "slow"}

    monkeypatch.setattr(chat, "get_graph", lambda: fake_graph(vector=slow_node))

    async def fire_all():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            start = time.perf_counter()
            await asyncio.gather(
                *[ac.post("/api/v1/chat", json={"question": f"q{i}"}) for i in range(N)]
            )
            return time.perf_counter() - start

    elapsed = asyncio.run(fire_all())
    assert elapsed < DELAY * (N / 2), (
        f"{N} concurrent requests took {elapsed:.2f}s against a {DELAY}s node — "
        "looks serialized, not concurrent"
    )


# --------------------------------------------------------------------------
# /proposals — list and detail over Trino
# --------------------------------------------------------------------------


CANNED_ROWS = [
    {
        "proposal_id": "0xabc",
        "protocol_name": "aave",
        "title": "[ARFC] Oracle Deprecation",
        "proposal_state": "closed",
        "proposal_created": "2026-07-21T00:00:00.000",
        "vote_count": 56,
    }
]


def test_list_proposals_returns_rows_from_trino(monkeypatch):
    monkeypatch.setattr(proposals, "trino_query", lambda sql: CANNED_ROWS)

    resp = client.get("/proposals", params={"protocol": "aave", "limit": 5})

    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["proposal_id"] == "0xabc"
    assert body[0]["protocol_name"] == "aave"


def test_list_proposals_rejects_an_unknown_protocol(monkeypatch):
    monkeypatch.setattr(proposals, "trino_query", lambda sql: CANNED_ROWS)

    resp = client.get("/proposals", params={"protocol": "not-a-real-dao"})

    assert resp.status_code == 400


def test_get_proposal_returns_the_matching_row(monkeypatch):
    monkeypatch.setattr(proposals, "trino_query", lambda sql: CANNED_ROWS)

    resp = client.get("/proposals/0xabc")

    assert resp.status_code == 200
    assert resp.json()["proposal_id"] == "0xabc"


def test_get_proposal_returns_404_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(proposals, "trino_query", lambda sql: [])

    resp = client.get("/proposals/0xdoesnotexist")

    assert resp.status_code == 404


def test_get_proposal_rejects_a_hostile_id_before_it_ever_reaches_trino(monkeypatch):
    """The SQL-injection-shaped id must be rejected by `render()` — status 400 — not passed
    through to Trino at all. Spying on trino_query proves it was never called."""
    calls: list[str] = []
    monkeypatch.setattr(proposals, "trino_query", lambda sql: calls.append(sql) or [])

    resp = client.get("/proposals/0x123' OR '1'='1")

    assert resp.status_code == 400
    assert calls == [], "a malformed id must never reach Trino"


# --------------------------------------------------------------------------
# The seam itself
# --------------------------------------------------------------------------


def test_get_graph_builds_and_caches_a_single_instance():
    """Regression guard for the singleton: building the real compiled graph must not touch
    the network or a database (it is node registration, not execution), and repeated calls
    must return the same object rather than recompiling per request."""
    graph_runtime._graph = None
    try:
        first = graph_runtime.get_graph()
        second = graph_runtime.get_graph()
        assert first is second
    finally:
        graph_runtime._graph = None
