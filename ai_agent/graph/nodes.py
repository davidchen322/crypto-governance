"""The three execution nodes. Each fetches material; none of them writes prose.

The vector node is where Phase 4's measurements become behaviour rather than a document:

  k by shape      thematic misses were a RANKING problem, not an absence problem —
                  recall 0.78 at k=5, 0.94 at k=20, 1.00 at k=50. Negatives verified clean
                  at 5/5 across k=5/10/20, because the accept/reject decision is computed
                  over the raw top-10 profile regardless of k.

  fan-out by scope  "which protocols do X" asks for COVERAGE, not ranking. One ranked list
                  cannot guarantee each protocol a fair look. Fan-out finds documents a flat
                  search never surfaces at any k — and, more usefully, can report which
                  protocols have NOTHING, which is half of what such a question asks.

  never fan out on scope="one"  measured: it costs documents (4/4 -> 3/4 on two eval
                  questions) because slots get reserved for protocols with nothing to say.
"""

from __future__ import annotations

from typing import Any

from ai_agent.chains.retrieval import search
from ai_agent.chains.trino_client import query as trino_query
from ai_agent.graph.llm import ROUTER_MODEL, complete_json
from ai_agent.graph.sql_templates import TemplateError, catalogue, render
from ai_agent.graph.state import AgentState
from config.protocols import PROTOCOLS

K_NARROW = 5  # a lookup-shaped question: one document is the answer
K_BROAD = 20  # a theme within one protocol: the answer is several documents
K_PER_PROTOCOL = 8  # fan-out depth; 8x5 reached 7/7 on the cross-protocol eval questions

SQL_PROMPT = """Pick the template that answers the question and fill its parameters.

TEMPLATES
{catalogue}

PARAMETER RULES
- protocol: one of aave, uniswap, arbitrum, optimism, ens — or null
- state: active, closed, pending — or null
- since: ISO date YYYY-MM-DD — or null
- field: vote_count or scores_total
- direction: ASC for earliest, DESC for latest
- limit: integer 1-100

QUESTION: {question}

Return ONLY JSON: {{"template":"<name>","params":{{...}}}}"""


def _k_for(state: AgentState) -> int:
    return K_NARROW if state.get("scope") == "one" and state.get("route") == "vector" else K_BROAD


def retrieve(state: AgentState, *, restrict_ids: set[str] | None = None) -> dict[str, Any]:
    """Vector retrieval, shaped by the router's scope decision."""
    question = state["question"]
    filters = state.get("filters") or {}
    protocol = filters.get("protocol")
    source = filters.get("source")

    # Cutoffs are disabled here on purpose. Phase 4 measured that the thresholds were fitted
    # on UNFILTERED searches: applying a protocol filter shrinks the candidate pool, makes
    # results more homogeneous, and narrows the gap for reasons unrelated to answerability.
    # Rather than re-fit per filter, retrieval returns candidates and the relevance JUDGE in
    # synthesis decides whether they answer the question. Judging measured decisively on the
    # pair no threshold separates (top score 0 vs 3).
    common = dict(max_distance=None, min_gap=None, source=source)

    if state.get("scope") == "all" and not protocol:
        found: list = []
        empty: list[str] = []
        for p in PROTOCOLS:
            hits = search(question, k=K_PER_PROTOCOL, protocol=p.name, **common)
            if hits:
                found.extend(hits)
            else:
                empty.append(p.label)
        found.sort(key=lambda r: r.distance)
        note = f"searched all {len(PROTOCOLS)} protocols"
        if empty:
            note += f"; no material found for {', '.join(empty)}"
        chunks = found
    else:
        chunks = search(question, k=_k_for(state), protocol=protocol, **common)
        note = f"searched {protocol or 'all protocols'} with k={_k_for(state)}"

    if restrict_ids is not None:
        before = len(chunks)
        # Narrow ONLY the source the SQL constraint actually ranged over. The templates query
        # proposal_versions, so their ids are proposal ids and no forum topic can ever match
        # one. Filtering everything by that set silently deletes all forum evidence — measured
        # on the demo question, which asks "what did the forum argue about them?" and got
        # "specific arguments from the forum are not provided in the evidence".
        chunks = [c for c in chunks if c.source != "proposal" or c.document_id in restrict_ids]
        kept_forum = sum(1 for c in chunks if c.source == "forum")
        note += (
            f"; narrowed proposals to the SQL result ({len(chunks)}/{before} chunks kept, "
            f"{kept_forum} of them forum)"
        )

    return {"chunks": chunks, "retrieval_note": note}


def run_sql(state: AgentState) -> dict[str, Any]:
    """Pick a template, validate it, run it. The model never emits SQL."""
    choice = complete_json(
        SQL_PROMPT.format(catalogue=catalogue(), question=state["question"]),
        model=ROUTER_MODEL,
    )
    params = choice.get("params") or {}
    filters = state.get("filters") or {}
    params.setdefault("protocol", filters.get("protocol"))
    params.setdefault("since", filters.get("since"))

    try:
        sql = render(str(choice.get("template", "")), params)
    except TemplateError as exc:
        return {"rows": [], "retrieval_note": f"SQL template rejected: {exc}"}

    rows = trino_query(sql)
    return {"rows": rows, "retrieval_note": f"ran template {choice.get('template')!r}"}


def sql_node(state: AgentState) -> dict[str, Any]:
    return run_sql(state)


def vector_node(state: AgentState) -> dict[str, Any]:
    return retrieve(state)


def hybrid_node(state: AgentState) -> dict[str, Any]:
    """SQL first, then semantic search WITHIN its result.

    The ordering is the whole point. Searching everything and hoping a date filter is honoured
    is not a filter — it is a wish. Getting the structured set first makes the constraint real,
    and the semantic pass then only ranks inside it.
    """
    out = run_sql(state)
    rows = out.get("rows") or []

    # Only narrow when SQL actually produced document identities. A count returns
    # {"n": 23} and narrowing to that would delete every chunk.
    ids = {str(r.get("proposal_id")) for r in rows if r.get("proposal_id")}
    titles = {str(r.get("title")) for r in rows if r.get("title")}
    restrict = ids if ids else None

    got = retrieve(state, restrict_ids=restrict)
    note = f"{out.get('retrieval_note', '')}; {got['retrieval_note']}"
    if titles and not ids:
        note += f"; SQL returned {len(titles)} titled row(s) but no ids, so no narrowing"
    return {"rows": rows, "chunks": got["chunks"], "retrieval_note": note}
