"""Phase 6 acceptance against the real stack.

`integration` = needs Postgres + Trino (docker compose up). `live` additionally spends a
fraction of a cent on an LLM call, matching the Phase 4/5 convention. These drive the actual
FastAPI app in-process via TestClient — the same ASGI app `make api` serves — rather than
starting a subprocess, so they exercise the real route code against the real stack without
needing a port free or a server already running.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend_api.main import app

pytestmark = pytest.mark.integration

client = TestClient(app)


def test_health_is_actually_ok_against_the_real_stack():
    resp = client.get("/health")
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["status"] == "ok"
    assert body["postgres"]["status"] == "ok"
    assert body["trino"]["status"] == "ok"


def test_health_survives_being_asked_before_any_data_exists():
    """A stack with empty silver tables is still a HEALTHY stack — health checks
    reachability, not data volume. Distinguishing those is the whole point of Phase 3's
    `is_current` guard existing separately from this endpoint."""
    resp = client.get("/health")
    assert resp.status_code == 200


def test_list_proposals_executes_against_real_trino_even_with_no_rows():
    """Whether or not silver has been built yet in this environment, the route must
    execute a real query and return a well-formed (possibly empty) list, not error."""
    resp = client.get("/proposals", params={"limit": 5})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_proposal_against_a_real_proposal_id_if_silver_has_one():
    """Finds a real id via the list endpoint first; skips rather than failing vacuously
    if silver is empty, matching the skip-with-a-reason convention from Phase 5's
    `test_is_current_matters_or_says_why_it_cannot_be_shown`."""
    listed = client.get("/proposals", params={"limit": 1}).json()
    if not listed:
        pytest.skip("silver holds no proposals in this environment yet — run `make silver`")

    proposal_id = listed[0]["proposal_id"]
    resp = client.get(f"/proposals/{proposal_id}")

    assert resp.status_code == 200
    detail = resp.json()
    assert detail["proposal_id"] == proposal_id
    assert detail["protocol_name"] == listed[0]["protocol_name"]


def test_get_proposal_404s_on_an_id_that_is_well_formed_but_absent():
    resp = client.get("/proposals/0xdoesnotexistanywhereinsilver00000000000000000000000000000000")
    assert resp.status_code == 404


@pytest.mark.live
def test_chat_answers_the_demo_question_through_the_http_layer():
    """The Phase 5 demo question, driven through the API instead of the CLI — proving the
    delivery layer changes nothing about what the agent does or does not know."""
    resp = client.post(
        "/api/v1/chat",
        json={
            "question": "Which Aave proposals in the last quarter changed risk parameters, "
            "and what did the forum argue about them?"
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["route"] == "hybrid"
    assert body["answer"] and not body["data_gap"]
    assert body["citations"], "no citations resolved"
    for c in body["citations"]:
        assert c["chunk_ref"] and c["document_id"]


@pytest.mark.live
def test_chat_stream_reaches_a_synthesis_event_against_the_real_agent():
    with client.stream(
        "GET",
        "/api/v1/chat/stream",
        params={"question": "How many proposals does each protocol have?"},
    ) as resp:
        assert resp.status_code == 200
        events = [line for line in resp.iter_lines() if line.startswith("event: ")]
    assert events[0] == "event: router"
    assert events[-1] == "event: synthesis"
