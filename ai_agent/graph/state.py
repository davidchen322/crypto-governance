"""The one dictionary every node in the graph reads and writes.

`messages` carries the `add_messages` reducer, and that annotation is the whole reason this
module exists separately rather than being a dict literal in workflow.py.

Without a reducer LangGraph OVERWRITES a key when a node returns it. For `messages` that
means conversation history is silently replaced on every node return instead of appended —
every turn looks like the first, and nothing raises. The original blueprint had a docstring
where the annotation belongs, which is a comment describing behaviour the code did not have.

The other keys are deliberately plain. They are per-question working state: the router writes
`route` and `filters`, an execution node writes `rows` and/or `chunks`, synthesis writes
`answer` and `citations`. Overwrite is the correct semantics for those — a second retrieval
pass should replace its results, not accumulate them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

Route = Literal["sql", "vector", "hybrid"]
Scope = Literal["one", "all"]


class RouteDecision(TypedDict, total=False):
    """What the router emits. Kept separate so it can be validated on its own."""

    route: Route
    scope: Scope
    protocol: str | None
    since: str | None
    source: str | None
    reason: str


class AgentState(TypedDict, total=False):
    # Accumulates. See the module docstring — this annotation is load-bearing.
    messages: Annotated[Sequence[BaseMessage], add_messages]

    question: str
    route: Route
    scope: Scope
    filters: dict[str, Any]

    rows: list[dict[str, Any]]  # from the SQL node
    chunks: list[Any]  # SearchResult objects from the vector node
    retrieval_note: str  # e.g. which protocols returned nothing

    answer: str
    citations: list[dict[str, Any]]
    data_gap: bool  # True when nothing retrieved supports an answer
