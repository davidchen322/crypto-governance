"""Phase 5 acceptance: state, topology, routing contract, SQL safety, citations.

No API key, no database, no docker. Every LLM call is replaced by a fake, so these run
anywhere and cost nothing. What they cover is the set of Phase 5 failures that are silent —
each one produces a fluent, confident, wrong result rather than an error.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass

import pytest

from ai_agent.graph import nodes, router, synthesis
from ai_agent.graph.sql_templates import TemplateError, render
from ai_agent.graph.state import AgentState
from ai_agent.graph.workflow import EXECUTION_NODES, build_graph, choose_branch


@dataclass
class FakeChunk:
    source: str = "proposal"
    document_id: str = "0xabc"
    protocol_name: str = "aave"
    chunk_index: int = 0
    heading: str | None = None
    text: str = "Aave — [ARFC] Oracle Deprecation\n\nThe reserves in scope share a reason."
    distance: float = 0.31
    title: str | None = "[ARFC] Oracle Deprecation"
    document_date: object = None


# --------------------------------------------------------------------------
# State — the reducer
# --------------------------------------------------------------------------


def test_messages_carries_the_add_messages_reducer():
    """The bug this pins: without a reducer LangGraph OVERWRITES `messages` on every node
    return instead of appending. Conversation history vanishes, every turn looks like the
    first, and nothing raises. The original blueprint had a docstring here instead."""
    hints = typing.get_type_hints(AgentState, include_extras=True)
    assert "add_messages" in str(hints["messages"])


def test_messages_actually_accumulate_through_the_graph():
    """Asserting on the annotation is not enough — this proves the behaviour."""
    from langchain_core.messages import AIMessage, HumanMessage

    g = build_graph(
        router=lambda s: {"route": "vector", "messages": [AIMessage(content="routed")]},
        sql=lambda s: {},
        vector=lambda s: {"messages": [AIMessage(content="retrieved")]},
        hybrid=lambda s: {},
        synthesis=lambda s: {"answer": "done", "messages": [AIMessage(content="answered")]},
    )
    out = g.invoke({"question": "q", "messages": [HumanMessage(content="asked")]})
    assert [m.content for m in out["messages"]] == ["asked", "routed", "retrieved", "answered"]


# --------------------------------------------------------------------------
# Topology — the three ways the blueprint's graph failed
# --------------------------------------------------------------------------


def test_graph_compiles_and_returns():
    """The blueprint never called .compile(), routed to three undefined nodes, and had no
    END edge — so the module raised on import and the API never started."""
    g = build_graph(
        router=lambda s: {"route": "sql"},
        sql=lambda s: {"rows": [{"n": 1}]},
        vector=lambda s: {"chunks": []},
        hybrid=lambda s: {},
        synthesis=lambda s: {"answer": "ok"},
    )
    out = g.invoke({"question": "q", "messages": []})
    assert out["answer"] == "ok" and out["rows"] == [{"n": 1}]


@pytest.mark.parametrize("route", EXECUTION_NODES)
def test_each_route_reaches_only_its_own_store(route):
    """A 'vector' question quietly running SQL is expensive and wrong, and nothing in the
    output would say so."""
    visited = []
    g = build_graph(
        router=lambda s: {"route": route},
        sql=lambda s: visited.append("sql") or {},
        vector=lambda s: visited.append("vector") or {},
        hybrid=lambda s: visited.append("hybrid") or {},
        synthesis=lambda s: {"answer": "ok"},
    )
    g.invoke({"question": "q", "messages": []})
    assert visited == [route]


def test_an_unroutable_question_degrades_instead_of_crashing():
    assert choose_branch({"route": "nonsense"}) == "vector"
    assert choose_branch({}) == "vector"


# --------------------------------------------------------------------------
# The routing contract
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["vector search", "", None, "postgres", "SELECT"])
def test_unknown_routes_fall_back_to_vector(bad):
    assert router.normalise({"route": bad})["route"] == "vector"


@pytest.mark.parametrize("variant", ["SQL", "Sql", " sql ", "HYBRID"])
def test_route_casing_and_whitespace_are_normalised_not_rejected(variant):
    """A model returning "SQL" means sql. Falling back to vector there would silently send
    a counting question to the wrong store — the failure this whole layer exists to prevent."""
    assert router.normalise({"route": variant})["route"] == variant.strip().lower()


def test_unknown_scope_defaults_to_the_safe_direction():
    """Too wide costs context; too narrow can exclude the answer outright."""
    assert router.normalise({"route": "vector", "scope": "???"})["scope"] == "all"


def test_a_named_protocol_forces_single_scope():
    """Measured in Phase 4: fanning out on a single-protocol question LOSES documents
    (4/4 -> 3/4 on two eval questions), because slots are reserved for protocols with
    nothing to say."""
    d = router.normalise({"route": "vector", "scope": "all", "protocol": "aave"})
    assert d["scope"] == "one" and d["protocol"] == "aave"


def test_unknown_protocols_and_sources_are_dropped_not_passed_through():
    d = router.normalise({"route": "vector", "protocol": "makerdao", "source": "twitter"})
    assert d["protocol"] is None and d["source"] is None


@pytest.mark.parametrize("bad", ["last quarter", "2026", "May 2026", 20260501])
def test_malformed_dates_are_dropped(bad):
    assert router.normalise({"route": "sql", "since": bad})["since"] is None


# --------------------------------------------------------------------------
# SQL safety
# --------------------------------------------------------------------------


def test_every_template_constrains_is_current():
    """Silver is SCD2. Without this a proposal edited three times is counted three times,
    and the inflated number looks plausible enough that nobody checks it."""
    from ai_agent.graph.sql_templates import TEMPLATES

    for name in TEMPLATES:
        params = {"field": "vote_count", "direction": "ASC", "limit": 5}
        assert "is_current" in render(name, params), name


@pytest.mark.parametrize(
    "evil",
    ["aave'; DROP TABLE silver.proposal_versions--", "' OR 1=1--", "x" * 200],
)
def test_injection_attempts_are_rejected_not_escaped(evil):
    """The model picks a template name and typed parameters; it never emits SQL."""
    with pytest.raises(TemplateError):
        render("list_proposals", {"protocol": evil})


def test_unknown_template_names_are_rejected():
    with pytest.raises(TemplateError):
        render("delete_everything", {})


def test_limits_are_clamped():
    assert "LIMIT 100" in render("top_proposals_by", {"field": "vote_count", "limit": 10_000})
    assert "LIMIT 1" in render("top_proposals_by", {"field": "vote_count", "limit": -5})


def test_enum_parameters_reject_free_text():
    with pytest.raises(TemplateError):
        render("top_proposals_by", {"field": "vote_count; DROP", "limit": 5})


# --------------------------------------------------------------------------
# Citations — the output contract
# --------------------------------------------------------------------------


def test_a_fabricated_marker_is_dropped_and_reported():
    """A hallucinated citation must fail a dictionary lookup rather than reach a reader."""
    _, index = synthesis._passage_block([FakeChunk()])
    kept, problems = synthesis.validate_citations("A claim [c0] and an invented one [c7].", index)
    assert [c["marker"] for c in kept] == ["c0"]
    assert any("c7" in p for p in problems)


def test_markers_resolve_to_the_document_actually_retrieved():
    chunks = [FakeChunk(document_id="0xaaa"), FakeChunk(document_id="0xbbb", chunk_index=3)]
    _, index = synthesis._passage_block(chunks)
    kept, problems = synthesis.validate_citations("First [c0], second [c1].", index)
    assert problems == []
    assert [c["document_id"] for c in kept] == ["0xaaa", "0xbbb"]
    assert kept[1]["chunk_ref"] == "proposal:0xbbb#3"


def test_the_model_never_supplies_a_source_identity():
    """The first version showed `[0] ref=proposal:0xaa68…#3` and asked the model to echo the
    ref. It cited "0" — the bracket index — and every citation in a fluent, authoritative
    answer failed to resolve. Markers are now selected from a list, not reproduced."""
    block, index = synthesis._passage_block([FakeChunk()])
    assert "[c0]" in block
    assert set(index) == {"c0"}
    _, problems = synthesis.validate_citations("Claim [0].", index)
    assert problems == [] or all("c" in p for p in problems)


def test_repeated_markers_are_reported_once():
    _, index = synthesis._passage_block([FakeChunk()])
    kept, _ = synthesis.validate_citations("[c0] and again [c0] and [c0].", index)
    assert len(kept) == 1


def test_an_answer_with_no_markers_yields_no_citations():
    _, index = synthesis._passage_block([FakeChunk()])
    kept, problems = synthesis.validate_citations("A confident claim with no source.", index)
    assert kept == [] and problems == []


# --------------------------------------------------------------------------
# DATA GAP
# --------------------------------------------------------------------------


def test_data_gap_when_the_judge_rejects_everything(monkeypatch):
    """The last line of defence. Phase 4 measured a chunk about REVENUE PROJECTIONS being
    retrieved for a question about FOUNDER COMPENSATION at a distance no threshold rejects."""
    monkeypatch.setattr(synthesis, "judge", lambda q, c: {0: 0, 1: 1})
    out = synthesis.synthesis_node(
        {"question": "How much is the Aave founder paid?", "chunks": [FakeChunk(), FakeChunk()]}
    )
    assert out["data_gap"] is True
    assert "DATA GAP" in out["answer"] and out["citations"] == []


def test_no_retrieval_at_all_is_a_data_gap(monkeypatch):
    monkeypatch.setattr(synthesis, "judge", lambda q, c: {})
    out = synthesis.synthesis_node({"question": "q", "chunks": [], "rows": []})
    assert out["data_gap"] is True


# --------------------------------------------------------------------------
# Retrieval shaping — Phase 4's measurements, as behaviour
# --------------------------------------------------------------------------


def test_scope_all_fans_out_across_every_protocol(monkeypatch):
    """'Which protocols do X' asks for COVERAGE. One ranked list cannot guarantee each
    protocol a fair look."""
    seen = []

    def fake_search(q, **kw):
        seen.append(kw.get("protocol"))
        return [FakeChunk(protocol_name=kw.get("protocol") or "?")]

    monkeypatch.setattr(nodes, "search", fake_search)
    out = nodes.retrieve(
        {"question": "which protocols pay delegates?", "scope": "all", "filters": {}}
    )
    assert sorted(x for x in seen if x) == ["aave", "arbitrum", "ens", "optimism", "uniswap"]
    assert len(out["chunks"]) == 5


def test_scope_one_does_not_fan_out(monkeypatch):
    """Measured: fanning out on a single-protocol question costs documents."""
    seen = []

    def fake_search(q, **kw):
        seen.append(kw.get("protocol"))
        return [FakeChunk()]

    monkeypatch.setattr(nodes, "search", fake_search)
    nodes.retrieve(
        {"question": "what is aave doing?", "scope": "one", "filters": {"protocol": "aave"}}
    )
    assert seen == ["aave"]


def test_fanout_reports_protocols_with_nothing(monkeypatch):
    """Half of what 'which protocols?' asks for is naming the ones with nothing to say —
    a flat ranked list structurally cannot report an absence."""
    monkeypatch.setattr(
        nodes, "search", lambda q, **kw: [FakeChunk()] if kw.get("protocol") == "aave" else []
    )
    out = nodes.retrieve({"question": "q", "scope": "all", "filters": {}})
    assert "no material found for" in out["retrieval_note"]
    assert "Uniswap" in out["retrieval_note"]


def test_narrowing_keeps_forum_evidence(monkeypatch):
    """The bug this pins: SQL templates query proposal_versions, so their ids are proposal
    ids and no forum topic can ever match one. Filtering everything by that set silently
    deleted all forum evidence — measured on the demo question, which asks 'what did the
    forum argue about them?' and got 'specific arguments from the forum are not provided'."""
    chunks = [
        FakeChunk(source="proposal", document_id="keep"),
        FakeChunk(source="proposal", document_id="drop"),
        FakeChunk(source="forum", document_id="25482"),
    ]
    monkeypatch.setattr(nodes, "search", lambda q, **kw: chunks)
    out = nodes.retrieve({"question": "q", "scope": "one", "filters": {}}, restrict_ids={"keep"})
    kept = {(c.source, c.document_id) for c in out["chunks"]}
    assert kept == {("proposal", "keep"), ("forum", "25482")}
