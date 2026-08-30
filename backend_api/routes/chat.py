"""`/api/v1/chat` — the FastAPI entry point onto the Phase 5 agent graph.

Deliberately thin. Every route here runs the compiled graph and shapes what it returns; the
actual reasoning — routing, retrieval, the relevance judge, synthesis, citation validation —
lives in `ai_agent.graph`, exactly as it does for `gov ask`. If an answer looks wrong here,
the agent is wrong; this layer only delivers it.

Two endpoints, not one, because of how they are meant to be driven:

    POST /api/v1/chat          one JSON response. This is what `/docs`' "Try it out" can
                                actually call, and what a non-streaming client wants.
    GET  /api/v1/chat/stream   Server-Sent Events, one event per completed graph node, so
                                a UI can show "routing... retrieving... answering..." instead
                                of a blank wait for the whole request.

What "stream" does NOT mean here: token-by-token streaming of the analyst's prose. That
would require `ai_agent/graph/llm.py` to use streaming chat completions instead of one
request/response call per node — a change to Phase 5, not a delivery layer over it.
Streaming graph-node progress is the honest middle ground: it is real and asynchronous, and
it does not pretend synthesis itself streams.

The route handler is `async def` and calls `await graph.ainvoke(...)` rather than a sync
`.invoke()` — the exact correction the implementation plan calls out against the original
blueprint. Every node in this graph is a plain synchronous function (it calls `requests` and
`psycopg` under the hood), and that is fine: LangGraph's `ainvoke`/`astream` run a node that
has no async implementation via a thread-pool executor rather than inline on the event loop,
so a slow node in one request does not stall another request's turn. That is not an
assumption — see `test_concurrent_chat_requests_do_not_serialize` in
`tests/test_phase6_api.py`, which proves it mechanically rather than trusting the framework.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from backend_api.graph_runtime import get_graph
from backend_api.schemas import ChatRequest, ChatResponse

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


def _initial_state(question: str) -> dict[str, Any]:
    return {"question": question, "messages": []}


def _chunk_to_dict(chunk: Any) -> dict[str, Any]:
    """Mirrors `gov search --json` / `gov ask --json` (`ai_agent/cli.py`), so a client built
    against either surface sees the same shape for a retrieved chunk."""
    date = getattr(chunk, "document_date", None)
    return {
        "source": chunk.source,
        "document_id": chunk.document_id,
        "protocol_name": chunk.protocol_name,
        "chunk_index": chunk.chunk_index,
        "heading": chunk.heading,
        "text": chunk.text,
        "distance": round(chunk.distance, 4),
        "title": chunk.title,
        "document_date": date.isoformat() if date else None,
    }


def _json_safe(update: dict[str, Any]) -> dict[str, Any]:
    """A graph node's return value, made safe for `json.dumps`.

    `messages` carries `BaseMessage` objects and is not part of the API's answer contract —
    the CLI's `--json` mode excludes it for the same reason. `chunks` carries `SearchResult`
    dataclasses, which need converting explicitly.
    """
    out = dict(update)
    if "chunks" in out:
        out["chunks"] = [_chunk_to_dict(c) for c in out["chunks"]]
    out.pop("messages", None)
    return out


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("", response_model=ChatResponse)
async def chat(body: ChatRequest) -> dict[str, Any]:
    """One question in, one analyst answer out."""
    graph = get_graph()
    state = await graph.ainvoke(_initial_state(body.question))
    return {
        "question": body.question,
        "route": state.get("route"),
        "scope": state.get("scope"),
        "filters": state.get("filters"),
        "answer": state.get("answer"),
        "data_gap": state.get("data_gap"),
        "citations": state.get("citations") or [],
        "retrieval_note": state.get("retrieval_note"),
    }


@router.get("/stream")
async def chat_stream(question: str) -> StreamingResponse:
    """The same question, delivered as one Server-Sent Event per completed graph node."""
    graph = get_graph()

    async def events():
        try:
            async for update in graph.astream(_initial_state(question), stream_mode="updates"):
                for node_name, payload in update.items():
                    yield _sse(node_name, _json_safe(payload))
        except Exception as exc:
            # A system boundary: an LLM call, a DB query or a Trino request can genuinely
            # fail. Reporting it as an SSE event beats letting the connection drop with no
            # visible reason, which is worse for a demo and worse in production.
            yield _sse("error", {"error": str(exc)})

    return StreamingResponse(events(), media_type="text/event-stream")
