"""Contract tests against the live Snapshot and Discourse APIs.

Marked `live` and excluded from the default run and from `make verify`: these depend on
someone else's uptime, and a red build caused by a third party's maintenance window teaches
nothing. Run them deliberately with `make verify-live` when changing a client, or when a
harvest starts returning something unexpected.

Their job is to detect API drift — a renamed field or a space id that has gone empty —
which is the failure mode most likely to silently break ingestion later.
"""

from __future__ import annotations

import pytest

from config.protocols import PROTOCOLS
from data_pipeline.extraction.discourse_client import DiscourseClient
from data_pipeline.extraction.http import HttpClient, TokenBucket
from data_pipeline.extraction.snapshot_client import SnapshotClient

pytestmark = [pytest.mark.integration, pytest.mark.live]


@pytest.fixture(scope="module")
def snapshot() -> SnapshotClient:
    return SnapshotClient(HttpClient(TokenBucket(30)))


def test_snapshot_endpoint_is_the_api_not_the_web_app(snapshot):
    """https://snapshot.org returns HTML. The blueprint pointed at it; this pins the fix."""
    assert snapshot.endpoint == "https://hub.snapshot.org/graphql"
    space = snapshot.fetch_space("aavedao.eth")
    assert space and space["id"] == "aavedao.eth"


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=lambda p: p.name)
def test_every_configured_snapshot_space_exists_and_is_populated(snapshot, protocol):
    """`aave.eth` looks plausible and returns nothing — an id that silently goes empty is
    indistinguishable from a working pipeline with no new proposals."""
    space = snapshot.fetch_space(protocol.snapshot_space)
    assert space is not None, f"space {protocol.snapshot_space} does not exist"
    assert space["proposalsCount"] > 0, f"space {protocol.snapshot_space} has no proposals"


def test_proposal_carries_the_fields_the_pipeline_depends_on(snapshot):
    proposals = snapshot.fetch_proposals("aavedao.eth", limit=1)
    assert proposals, "no proposals returned"
    proposal = proposals[0]

    for field in ("id", "title", "body", "state", "created", "author", "space"):
        assert field in proposal, f"missing field {field!r}"

    # `state`, not `status` — asking for a nonexistent field fails the entire query.
    assert proposal["state"] in {"pending", "active", "closed"}
    # The join key to Discourse in Phase 3.
    assert "discussion" in proposal


def test_pagination_advances_rather_than_repeating(snapshot):
    """A stalled cursor would silently return the same page forever."""
    proposals = snapshot.fetch_proposals("aavedao.eth", limit=40, page_size=10)
    ids = [p["id"] for p in proposals]
    assert len(ids) > 10, "pagination did not go past the first page"
    assert len(ids) == len(set(ids)), "pagination returned duplicates"


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=lambda p: p.name)
def test_every_configured_forum_serves_topics(protocol):
    client = DiscourseClient(protocol.discourse_host, HttpClient(TokenBucket(20)))
    topics = client.fetch_topics(limit=5)
    assert topics, f"{protocol.discourse_host} returned no topics"
    assert {"id", "title", "slug"} <= set(topics[0])


def test_discourse_topic_detail_includes_post_bodies():
    client = DiscourseClient("gov.uniswap.org", HttpClient(TokenBucket(20)))
    topic_id = client.fetch_topics(limit=1)[0]["id"]
    detail = client.topic(topic_id)
    posts = detail.get("post_stream", {}).get("posts", [])
    assert posts, "topic detail carried no posts"
    assert posts[0].get("cooked"), "post body missing"
