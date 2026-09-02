"""Trino query retry, against a fake transport — no real Trino, no network.

Found by running the real integration suite repeatedly (see ai_agent/chains/trino_client.py's
own comment): under load, Trino intermittently abandons a query the client never actually
stopped polling. This pins the fix without depending on that timing actually reproducing.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ai_agent.chains.trino_client import MAX_ATTEMPTS, TrinoError, query


def _response(json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


def _complete(rows: list[list]) -> dict:
    return {"columns": [{"name": "n"}], "data": rows}


ABANDONED = {"error": {"message": "Query xyz was abandoned by the client, as it may have exited"}}


def test_a_genuine_sql_error_is_not_retried():
    """The whole point of matching on 'abandoned' specifically: a real error must fail
    immediately, not disappear behind MAX_ATTEMPTS retries of the identical bad query."""
    with patch("ai_agent.chains.trino_client.requests") as mocked:
        mocked.post.return_value = _response({"error": {"message": "SYNTAX_ERROR: bad SQL"}})
        with pytest.raises(TrinoError, match="SYNTAX_ERROR"):
            query("SELECT nonsense")
    assert mocked.post.call_count == 1, "a non-abandoned error must not be retried"


def test_an_abandoned_query_is_retried_and_can_succeed():
    with (
        patch("ai_agent.chains.trino_client.requests") as mocked,
        patch("ai_agent.chains.trino_client.time.sleep"),
    ):
        mocked.post.side_effect = [
            _response(ABANDONED),
            _response(_complete([[1]])),
        ]
        rows = query("SELECT count(*) AS n FROM t")
    assert rows == [{"n": 1}]
    assert mocked.post.call_count == 2, "the whole query must be re-sent, not just a page"


def test_abandonment_on_every_attempt_still_raises():
    with (
        patch("ai_agent.chains.trino_client.requests") as mocked,
        patch("ai_agent.chains.trino_client.time.sleep"),
    ):
        mocked.post.return_value = _response(ABANDONED)
        with pytest.raises(TrinoError, match="abandoned"):
            query("SELECT 1")
    assert mocked.post.call_count == MAX_ATTEMPTS, "must stop retrying after MAX_ATTEMPTS"


def test_a_successful_first_attempt_never_retries():
    with patch("ai_agent.chains.trino_client.requests") as mocked:
        mocked.post.return_value = _response(_complete([[42]]))
        rows = query("SELECT n FROM t")
    assert rows == [{"n": 42}]
    assert mocked.post.call_count == 1


def test_pagination_across_nexturi_still_works_after_the_retry_change():
    """The retry wraps the whole query; it must not disturb the existing nextUri chase
    for a query that legitimately spans multiple pages."""
    with (
        patch("ai_agent.chains.trino_client.requests") as mocked,
        patch("ai_agent.chains.trino_client.time.sleep"),
    ):
        page1 = _response({"columns": [{"name": "n"}], "data": [[1]], "nextUri": "http://x/2"})
        page2 = _response({"data": [[2]]})
        mocked.post.return_value = page1
        mocked.get.return_value = page2
        rows = query("SELECT n FROM t")
    assert rows == [{"n": 1}, {"n": 2}]
