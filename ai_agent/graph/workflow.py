"""The compiled agent graph.

Shape:

    question -> router -> { sql | vector | hybrid } -> synthesis -> END

The router is a node that writes a decision into state; the branching itself is a conditional
edge reading that decision. Keeping those separate means the routing decision can be tested
without running any execution node, which is what the routing eval set scores.

Three failure modes this file exists to prevent, all of which the original blueprint shipped:

  * never calling `.compile()`      — a StateGraph that is never compiled cannot be invoked
  * edges pointing at undefined nodes — raises at build time, so the module cannot be imported
  * no terminal edge                — the graph has no END and never returns

`build_graph()` takes the node callables as arguments so tests can compile the real topology
with canned nodes. The topology is what this module is responsible for; the nodes are not.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, StateGraph

from ai_agent.graph.state import AgentState

NODE_ROUTER = "router"
NODE_SQL = "sql"
NODE_VECTOR = "vector"
NODE_HYBRID = "hybrid"
NODE_SYNTHESIS = "synthesis"

EXECUTION_NODES = (NODE_SQL, NODE_VECTOR, NODE_HYBRID)


def choose_branch(state: AgentState) -> str:
    """Conditional edge: read the decision the router already made.

    Defaults to the vector branch rather than raising. An unroutable question should still
    get a best-effort answer; a KeyError here would turn a bad guess into a crash.
    """
    route = state.get("route", NODE_VECTOR)
    return route if route in EXECUTION_NODES else NODE_VECTOR


def build_graph(
    router: Callable[[AgentState], dict[str, Any]],
    sql: Callable[[AgentState], dict[str, Any]],
    vector: Callable[[AgentState], dict[str, Any]],
    hybrid: Callable[[AgentState], dict[str, Any]],
    synthesis: Callable[[AgentState], dict[str, Any]],
):
    """Wire and compile the graph. Nodes are injected so the topology is testable alone."""
    g = StateGraph(AgentState)

    g.add_node(NODE_ROUTER, router)
    g.add_node(NODE_SQL, sql)
    g.add_node(NODE_VECTOR, vector)
    g.add_node(NODE_HYBRID, hybrid)
    g.add_node(NODE_SYNTHESIS, synthesis)

    g.set_entry_point(NODE_ROUTER)
    g.add_conditional_edges(
        NODE_ROUTER,
        choose_branch,
        {NODE_SQL: NODE_SQL, NODE_VECTOR: NODE_VECTOR, NODE_HYBRID: NODE_HYBRID},
    )
    # Every execution branch converges on synthesis, and synthesis terminates. Without the
    # END edge the graph would run and never return.
    for node in EXECUTION_NODES:
        g.add_edge(node, NODE_SYNTHESIS)
    g.add_edge(NODE_SYNTHESIS, END)

    return g.compile()


def default_graph():
    """The real graph, with the real nodes. Imported by the CLI."""
    from ai_agent.graph.nodes import hybrid_node, sql_node, vector_node
    from ai_agent.graph.router import router_node
    from ai_agent.graph.synthesis import synthesis_node

    return build_graph(router_node, sql_node, vector_node, hybrid_node, synthesis_node)
