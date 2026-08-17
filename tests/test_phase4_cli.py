"""Phase 4 acceptance: the `gov` CLI.

No database, no API — the CLI is a formatting layer, so it is tested against constructed
`SearchResult`s. What matters here is that it renders the three things the Phase 4 demo
promises (protocol, proposal, date) and that it does not lie when retrieval returns nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ai_agent import cli
from ai_agent.chains.retrieval import SearchResult


def make_result(**over) -> SearchResult:
    base = dict(
        source="proposal",
        document_id="0xabc123",
        protocol_name="aave",
        chunk_index=1,
        heading="Motivation",
        text="Aave — [ARFC] Oracle Deprecation\n\nThe reserves in scope share a reason. " * 8,
        distance=0.31,
        title="[ARFC] Oracle Deprecation for Long-tail Assets",
        document_date=datetime(2026, 8, 11, tzinfo=UTC),
    )
    base.update(over)
    return SearchResult(**base)


@pytest.fixture(autouse=True)
def no_colour():
    cli._plain()


def run(monkeypatch, argv, results):
    monkeypatch.setattr(cli, "search", lambda *a, **k: results)
    return cli.main(argv)


# --------------------------------------------------------------------------
# The demo's contract
# --------------------------------------------------------------------------


def test_demo_renders_protocol_proposal_and_date(monkeypatch, capsys):
    """The Phase 4 exit demo promises exactly these three fields. This is that promise."""
    run(monkeypatch, ["search", "delegate voting power"], [make_result()])
    out = capsys.readouterr().out
    assert "Aave" in out, "protocol missing"
    assert "[ARFC] Oracle Deprecation for Long-tail Assets" in out, "proposal title missing"
    assert "2026-08-11" in out, "date missing"


def test_the_date_shown_is_the_governance_date_not_the_harvest_date(monkeypatch, capsys):
    """The trap this pins: `valid_from` is when the pipeline observed a row, so on a
    single-pass harvest every row shares it. Rendering that as "date" would print the
    harvest date on every result and look entirely plausible. The CLI must read
    `document_date`, which comes from proposal_created / post_created_at."""
    run(monkeypatch, ["search", "q"], [make_result(document_date=datetime(2025, 1, 2))])
    assert "2025-01-02" in capsys.readouterr().out


def test_a_missing_date_is_labelled_not_blank(monkeypatch, capsys):
    run(monkeypatch, ["search", "q"], [make_result(document_date=None)])
    assert "unknown date" in capsys.readouterr().out


def test_protocol_is_shown_with_its_display_label(monkeypatch, capsys):
    """`ens` is stored lowercase; a human-facing listing should say ENS."""
    run(monkeypatch, ["search", "q"], [make_result(protocol_name="ens")])
    assert "ENS" in capsys.readouterr().out


def test_an_unknown_protocol_falls_back_instead_of_raising(monkeypatch, capsys):
    """A protocol added to the corpus but not yet to config/protocols.py must not crash a
    listing — the row still exists and is still worth showing."""
    run(monkeypatch, ["search", "q"], [make_result(protocol_name="makerdao")])
    assert "makerdao" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Saying "nothing" honestly
# --------------------------------------------------------------------------


def test_empty_results_explain_themselves_and_exit_zero(monkeypatch, capsys):
    """An empty result is the two-signal threshold working, not a failure. It exits 0 and
    says how to see the raw ranking — otherwise the only reading available to the user is
    'the tool is broken'."""
    code = run(monkeypatch, ["search", "what is the price of UNI"], [])
    out = capsys.readouterr().out
    assert code == 0
    assert "no sufficiently relevant chunks" in out
    assert "--no-threshold" in out


def test_no_threshold_disables_both_cutoffs(monkeypatch):
    """Disabling only max_distance leaves min_gap filtering, which is precisely the bug that
    made an integration test's 'unfiltered' control arm return nothing."""
    seen = {}

    def fake(query, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(cli, "search", fake)
    cli.main(["search", "q", "--no-threshold"])
    assert seen["max_distance"] is None and seen["min_gap"] is None


def test_thresholds_are_on_by_default(monkeypatch):
    seen = {}

    def fake(query, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(cli, "search", fake)
    cli.main(["search", "q"])
    assert seen["max_distance"] is not None and seen["min_gap"] is not None


# --------------------------------------------------------------------------
# Machine-readable output
# --------------------------------------------------------------------------


def test_json_output_is_parseable_and_dates_are_iso(monkeypatch, capsys):
    run(monkeypatch, ["search", "q", "--json"], [make_result()])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["protocol_name"] == "aave"
    assert payload[0]["document_date"].startswith("2026-08-11")
    assert payload[0]["distance"] == 0.31


def test_json_output_carries_no_ansi_escapes(monkeypatch, capsys):
    """JSON piped into another tool must not contain colour codes."""
    run(monkeypatch, ["search", "q", "--json"], [make_result()])
    assert "\033[" not in capsys.readouterr().out


def test_json_empty_results_is_an_empty_array(monkeypatch, capsys):
    run(monkeypatch, ["search", "q", "--json"], [])
    assert json.loads(capsys.readouterr().out) == []


# --------------------------------------------------------------------------
# Argument plumbing
# --------------------------------------------------------------------------


def test_filters_reach_the_search_layer(monkeypatch):
    seen = {}

    def fake(query, **kw):
        seen.update(kw)
        seen["query"] = query
        return []

    monkeypatch.setattr(cli, "search", fake)
    cli.main(["search", "treasury", "--protocol", "arbitrum", "--source", "forum", "-k", "9"])
    assert seen["query"] == "treasury"
    assert seen["protocol"] == "arbitrum"
    assert seen["source"] == "forum"
    assert seen["k"] == 9


def test_as_of_is_parsed_into_a_datetime(monkeypatch):
    seen = {}

    def fake(query, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(cli, "search", fake)
    cli.main(["search", "q", "--as-of", "2026-03-01T00:00:00"])
    assert seen["as_of"] == datetime(2026, 3, 1)


def test_an_unknown_protocol_is_rejected_at_the_parser():
    """Better a usage error than a silent empty result that reads as 'no coverage'."""
    with pytest.raises(SystemExit):
        cli.main(["search", "q", "--protocol", "nosuchdao"])


# --------------------------------------------------------------------------
# Snippet formatting
# --------------------------------------------------------------------------


def test_snippet_is_bounded_and_marks_truncation():
    lines = cli._snippet("word " * 400, width=40, lines=3)
    assert len(lines) <= 3
    assert all(len(x) <= 44 for x in lines)
    assert lines[-1].endswith("...")


def test_short_text_is_not_marked_truncated():
    assert cli._snippet("a short chunk", width=80, lines=3) == ["a short chunk"]
