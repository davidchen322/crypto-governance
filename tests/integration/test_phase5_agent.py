"""Phase 5 acceptance against the real stack.

`integration` = needs Postgres/Trino. `live` = additionally spends a fraction of a cent on
the API. Split so the free ones run anywhere, matching the Phase 4 convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_agent.chains.trino_client import query as trino_query
from ai_agent.graph.sql_templates import render

pytestmark = pytest.mark.integration

ROUTING = Path(__file__).resolve().parents[1] / "eval" / "routing.yaml"


# --------------------------------------------------------------------------
# The eval set itself must be well-formed — it is the instrument
# --------------------------------------------------------------------------


def test_routing_eval_set_is_balanced_and_well_formed():
    qs = yaml.safe_load(ROUTING.read_text())
    assert len(qs) == 15
    assert len({q["id"] for q in qs}) == 15, "duplicate ids"
    counts = {r: sum(1 for q in qs if q["route"] == r) for r in ("sql", "vector", "hybrid")}
    assert counts == {"sql": 5, "vector": 5, "hybrid": 5}, counts
    for q in qs:
        assert q.get("why"), f"{q['id']} has no rationale"
        if q["route"] in ("vector", "hybrid"):
            assert q.get("scope") in ("one", "all"), f"{q['id']} needs a scope"


def test_scope_one_is_only_used_when_a_protocol_is_identifiable():
    """The router reads ONLY the question. A label of scope="one" on a question naming no
    protocol demands corpus knowledge the router structurally cannot have — which is exactly
    the mistake made on route-oracle-deprecation in the first run of this eval set."""
    from config.protocols import BY_NAME

    names = {n for n in BY_NAME} | {p.label.lower() for p in BY_NAME.values()}
    for q in yaml.safe_load(ROUTING.read_text()):
        if q.get("scope") == "one":
            text = q["question"].lower()
            assert any(n in text for n in names), (
                f"{q['id']} is labelled scope=one but names no protocol"
            )


# --------------------------------------------------------------------------
# SQL templates against the real lakehouse
# --------------------------------------------------------------------------


def test_every_template_executes_against_silver():
    """A template that renders but does not run is a runtime failure discovered by a user."""
    from ai_agent.graph.sql_templates import TEMPLATES

    for name in TEMPLATES:
        sql = render(name, {"field": "vote_count", "direction": "DESC", "limit": 3})
        rows = trino_query(sql)
        assert isinstance(rows, list), name


def test_is_current_matters_or_says_why_it_cannot_be_shown():
    """The `is_current` guard in every SQL template protects against SCD2 double-counting.

    Right now it cannot be DEMONSTRATED on this data: the corpus has been harvested once and
    no document has changed, so silver holds 0 superseded rows in either table and the
    predicate filters nothing. That is worth stating out loud rather than asserting a
    differential that happens to hold — a test that passes because the feature is inert is
    the kind of vacuous green this project keeps finding.

    So: skip explicitly (visible in the run as `s`, not as a pass) until a re-harvest
    produces a changed document, at which point this becomes a real differential test."""
    total = trino_query("SELECT count(*) AS n FROM iceberg.silver.proposal_versions")[0]["n"]
    current = trino_query(
        "SELECT count(*) AS n FROM iceberg.silver.proposal_versions WHERE is_current"
    )[0]["n"]
    assert current <= total, "more current rows than total rows is impossible"
    if total == current:
        pytest.skip(
            f"silver holds 0 superseded rows ({total} total). The guard is protection "
            "against a future re-harvest, not a presently-demonstrable filter."
        )
    assert total > current


def test_list_proposals_returns_identities_for_hybrid_narrowing():
    """Hybrid narrows the vector pass to the SQL result. Without proposal_id in the
    projection that narrowing silently cannot happen, and the date filter becomes a wish."""
    rows = trino_query(render("list_proposals", {"protocol": "aave", "limit": 3}))
    assert rows and all(r.get("proposal_id") for r in rows)


# --------------------------------------------------------------------------
# End to end — these spend money
# --------------------------------------------------------------------------


@pytest.mark.live
def test_router_meets_the_exit_criterion():
    """Exit criterion from the plan: >= 80% on the 15-question routing set."""
    from ai_agent.graph.router import decide

    qs = yaml.safe_load(ROUTING.read_text())
    correct = sum(decide(q["question"])["route"] == q["route"] for q in qs)
    assert correct / len(qs) >= 0.80, f"routing accuracy {correct}/{len(qs)}"


@pytest.mark.live
def test_a_counting_question_routes_to_sql_and_returns_rows():
    from ai_agent.graph.workflow import default_graph

    out = default_graph().invoke(
        {"question": "How many proposals does each protocol have?", "messages": []}
    )
    assert out["route"] == "sql"
    assert out.get("rows"), "SQL branch returned no rows"
    assert not out.get("data_gap")


@pytest.mark.live
def test_the_demo_question_answers_with_resolvable_citations():
    """The Phase 5 demo. Every citation must resolve to a chunk actually retrieved."""
    from ai_agent.graph.workflow import default_graph

    out = default_graph().invoke(
        {
            "question": "Which Aave proposals in the last quarter changed risk parameters, "
            "and what did the forum argue about them?",
            "messages": [],
        }
    )
    assert out["route"] == "hybrid"
    assert out["answer"] and not out.get("data_gap")
    cites = out["citations"]
    assert cites, "no citations resolved"
    for c in cites:
        assert c["chunk_ref"] and c["document_id"] and c["source"] in ("proposal", "forum")


@pytest.mark.live
def test_relative_dates_resolve_against_today_not_an_invented_year():
    """Measured bug: with no anchor, "the last quarter" resolved to 2023-07-01 on a corpus
    whose newest proposal is from 2026."""
    from datetime import date

    from ai_agent.graph.router import decide

    since = decide("What has Uniswap proposed in the last quarter?")["since"]
    assert since, "no date extracted"
    assert int(since[:4]) >= date.today().year - 1, f"resolved to {since}"


@pytest.mark.live
def test_an_unanswerable_question_is_refused_rather_than_answered():
    """The judge is the last line of defence for the one case no distance threshold rejects."""
    from ai_agent.graph.workflow import default_graph

    out = default_graph().invoke({"question": "How much is the Aave founder paid?", "messages": []})
    assert out.get("data_gap") is True, f"answered anyway: {out.get('answer')!r}"


@pytest.mark.live
def test_a_cross_protocol_question_searches_every_protocol():
    from ai_agent.graph.workflow import default_graph

    out = default_graph().invoke(
        {"question": "Which protocols are running programs to pay delegates?", "messages": []}
    )
    assert out["scope"] == "all"
    assert "searched all" in (out.get("retrieval_note") or "")
