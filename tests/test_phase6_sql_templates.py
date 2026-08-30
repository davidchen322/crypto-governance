"""Phase 6 acceptance: the `get_proposal` template and its `proposal_id` validation.

No database, no network. `render()` is a pure string-building function; what matters is that
it rejects the input shapes an HTTP client can send that a model-extracted parameter never
could — quotes, whitespace, SQL punctuation — since `proposal_id` is the first template
parameter that arrives directly from a URL path segment rather than from the router's LLM.
"""

from __future__ import annotations

import pytest

from ai_agent.graph.sql_templates import TemplateError, render


def test_get_proposal_renders_with_a_valid_id():
    sql = render("get_proposal", {"proposal_id": "0x4ec0c13baf55472ecd53"})
    assert "proposal_id = '0x4ec0c13baf55472ecd53'" in sql
    assert "is_current" in sql


def test_get_proposal_accepts_a_full_length_snapshot_style_id():
    """0x + 64 hex chars = 66 characters — the actual shape of a Snapshot proposal id."""
    full_id = "0x" + "a1" * 32
    sql = render("get_proposal", {"proposal_id": full_id})
    assert full_id in sql


@pytest.mark.parametrize(
    "bad_id",
    [
        "0x123' OR '1'='1",  # classic injection shape
        "0x123; DROP TABLE proposal_versions;--",
        "has a space",
        'quote"inside',
        "",
        "x" * 200,  # exceeds the length cap
    ],
)
def test_get_proposal_rejects_hostile_or_malformed_ids(bad_id: str):
    with pytest.raises(TemplateError):
        render("get_proposal", {"proposal_id": bad_id})


def test_get_proposal_rejects_a_missing_id():
    with pytest.raises(TemplateError):
        render("get_proposal", {})


def test_get_proposal_rejects_a_non_string_id():
    with pytest.raises(TemplateError):
        render("get_proposal", {"proposal_id": 12345})


def test_quote_length_cap_still_admits_the_widened_ceiling():
    """The general string cap moved from 64 to 128 to admit a 66-char proposal id without
    weakening the enum-validated parameters, which never approach either ceiling."""
    sql = render("list_proposals", {"protocol": "aave", "limit": 5})
    assert "aave" in sql
