"""The one seam between the API routes and the compiled Phase 5 graph.

Building the graph is cheap — it is node registration, not a network call — but there is no
reason to redo it on every request, and routes calling `ai_agent.graph.workflow.default_graph`
directly would give tests nothing to monkeypatch except langgraph internals. Routes call
`get_graph()` instead; tests replace it with a `build_graph(...)` wired from fake nodes, the
same pattern `tests/test_phase5_graph.py` already uses for the graph itself.
"""

from __future__ import annotations

from typing import Any

_graph: Any = None


def get_graph() -> Any:
    global _graph
    if _graph is None:
        from ai_agent.graph.workflow import default_graph

        _graph = default_graph()
    return _graph
