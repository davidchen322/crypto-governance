"""Phase 2 acceptance: rate limiting, retry, and content addressing.

No network and no docker. The 429 path is exercised with a fake transport rather than by
actually getting rate-limited, so the exit criterion ("a 429 is retried, not fatal") is
deterministic instead of dependent on someone else's traffic shaping.
"""

from __future__ import annotations

import pytest
import requests

from data_pipeline.extraction.bronze import bronze_key, canonical_hash, prune
from data_pipeline.extraction.discourse_client import VOLATILE_FIELDS
from data_pipeline.extraction.http import HttpClient, RateLimitError, TokenBucket


class FakeClock:
    """Monotonic clock that only advances when sleep() is called."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeResponse:
    """Mimics the parts of requests.Response the client touches, including
    raise_for_status — a double that silently lacks it would let a broken error path pass."""

    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.headers = headers or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


class FakeSession:
    """Replays a scripted sequence of responses and records what was asked for."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if not self._responses:
            raise AssertionError("FakeSession ran out of scripted responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


def test_token_bucket_allows_burst_up_to_capacity():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_minute=60, capacity=5, clock=clock.time, sleep=clock.sleep)
    for _ in range(5):
        bucket.acquire()
    assert clock.slept == [], "a full bucket should not block"


def test_token_bucket_throttles_once_drained():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_minute=60, capacity=2, clock=clock.time, sleep=clock.sleep)
    bucket.acquire()
    bucket.acquire()
    bucket.acquire()  # must wait for a refill
    assert clock.slept, "acquiring beyond capacity should sleep"
    # 60/minute == 1/second, so the wait is about a second.
    assert 0.5 <= sum(clock.slept) <= 1.5


def test_token_bucket_refills_over_time():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_minute=60, capacity=2, clock=clock.time, sleep=clock.sleep)
    bucket.acquire()
    bucket.acquire()
    clock.sleep(10)  # ten seconds of idling refills the bucket
    clock.slept.clear()
    bucket.acquire()
    assert clock.slept == [], "a refilled bucket should not block"


# --------------------------------------------------------------------------
# Retry — the Phase 2 exit criterion
# --------------------------------------------------------------------------


def test_429_is_retried_and_then_succeeds():
    clock = FakeClock()
    session = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": "2"}),
            FakeResponse(200, {"data": "ok"}),
        ]
    )
    client = HttpClient(
        bucket=TokenBucket(6000, clock=clock.time, sleep=clock.sleep),
        session=session,
        sleep=clock.sleep,
    )
    assert client.request_json("GET", "https://example.test/x") == {"data": "ok"}
    assert len(session.calls) == 2, "the request should have been retried once"
    assert 2 in clock.slept, "Retry-After: 2 should be honoured verbatim"


def test_retry_after_absent_falls_back_to_exponential_backoff():
    clock = FakeClock()
    session = FakeSession(
        [
            FakeResponse(429),
            FakeResponse(429),
            FakeResponse(200, {"data": "ok"}),
        ]
    )
    client = HttpClient(
        bucket=TokenBucket(6000, clock=clock.time, sleep=clock.sleep),
        session=session,
        sleep=clock.sleep,
        backoff_base=1.0,
    )
    client.request_json("GET", "https://example.test/x")
    waits = [w for w in clock.slept if w > 0]
    assert len(waits) >= 2
    assert waits[1] > waits[0], f"backoff should grow, got {waits}"


def test_server_errors_are_retried():
    clock = FakeClock()
    session = FakeSession([FakeResponse(503), FakeResponse(200, {"data": "ok"})])
    client = HttpClient(
        bucket=TokenBucket(6000, clock=clock.time, sleep=clock.sleep),
        session=session,
        sleep=clock.sleep,
    )
    assert client.request_json("GET", "https://example.test/x") == {"data": "ok"}


def test_connection_errors_are_retried():
    clock = FakeClock()
    session = FakeSession(
        [
            requests.ConnectionError("reset by peer"),
            FakeResponse(200, {"data": "ok"}),
        ]
    )
    client = HttpClient(
        bucket=TokenBucket(6000, clock=clock.time, sleep=clock.sleep),
        session=session,
        sleep=clock.sleep,
    )
    assert client.request_json("GET", "https://example.test/x") == {"data": "ok"}


def test_persistent_429_eventually_raises_rather_than_looping_forever():
    clock = FakeClock()
    session = FakeSession([FakeResponse(429) for _ in range(10)])
    client = HttpClient(
        bucket=TokenBucket(6000, clock=clock.time, sleep=clock.sleep),
        session=session,
        sleep=clock.sleep,
        max_attempts=3,
    )
    with pytest.raises(RateLimitError):
        client.request_json("GET", "https://example.test/x")
    assert len(session.calls) == 3


def test_client_errors_are_not_retried():
    """A 404 is a bug, not congestion — retrying it just wastes the rate budget."""
    clock = FakeClock()
    session = FakeSession([FakeResponse(404)])
    client = HttpClient(
        bucket=TokenBucket(6000, clock=clock.time, sleep=clock.sleep),
        session=session,
        sleep=clock.sleep,
    )
    with pytest.raises(requests.HTTPError):
        client.request_json("GET", "https://example.test/x")
    assert len(session.calls) == 1


# --------------------------------------------------------------------------
# Content addressing
# --------------------------------------------------------------------------


def test_hash_is_stable_across_key_order():
    """Dict ordering must not change the digest, or every re-harvest looks like an edit."""
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})


def test_hash_changes_when_content_changes():
    assert canonical_hash({"title": "x"}) != canonical_hash({"title": "y"})


def test_hash_is_stable_for_nested_and_unicode_content():
    left = {"body": "Fee switch — 0.05%", "meta": {"z": [1, 2], "a": None}}
    right = {"meta": {"a": None, "z": [1, 2]}, "body": "Fee switch — 0.05%"}
    assert canonical_hash(left) == canonical_hash(right)


def test_bronze_key_is_content_addressed_not_date_partitioned():
    """A date in the key would make tomorrow's unchanged re-harvest a duplicate write."""
    payload = {"id": "0xabc", "title": "Deprecate oracle"}
    first = bronze_key("snapshot", "aavedao.eth", "0xabc", canonical_hash(payload))
    second = bronze_key("snapshot", "aavedao.eth", "0xabc", canonical_hash(payload))
    assert first == second
    assert "dt=" not in first and "date" not in first


def test_bronze_key_changes_when_content_changes():
    a = bronze_key("snapshot", "aavedao.eth", "0xabc", canonical_hash({"title": "v1"}))
    b = bronze_key("snapshot", "aavedao.eth", "0xabc", canonical_hash({"title": "v2"}))
    assert a != b, "an edited document must land at a new key, preserving the old version"


def test_bronze_key_is_prefix_navigable():
    key = bronze_key("snapshot", "aavedao.eth", "0xabc", "deadbeef")
    assert key.startswith("snapshot/space=aavedao.eth/")
    assert key.endswith(".json")


def test_bronze_key_escapes_path_hostile_entity_ids():
    """Discourse slugs and URLs can contain slashes; they must not create phantom prefixes."""
    key = bronze_key("discourse", "gov.uniswap.org", "t/some-slug/12345", "cafe1234")
    assert key.count("/") == 3, f"entity id leaked path separators: {key}"


def test_volatile_fields_are_excluded_from_identity():
    """Discourse decorates every response with a randomised suggested_topics sidebar and a
    view counter. Hashing those made an unedited topic look edited on every harvest."""
    a = {"id": 1, "title": "Fee switch", "views": 100, "suggested_topics": [{"id": 9}]}
    b = {"id": 1, "title": "Fee switch", "views": 137, "suggested_topics": [{"id": 4}]}
    assert canonical_hash(a) != canonical_hash(b), "precondition: raw payloads differ"
    assert canonical_hash(a, VOLATILE_FIELDS) == canonical_hash(b, VOLATILE_FIELDS)


def test_real_content_edits_still_change_identity_despite_exclusions():
    """The projection must not be so aggressive that genuine edits stop registering."""
    a = {"id": 1, "title": "Fee switch", "views": 100}
    b = {"id": 1, "title": "Fee switch (revised)", "views": 100}
    assert canonical_hash(a, VOLATILE_FIELDS) != canonical_hash(b, VOLATILE_FIELDS)


def test_new_replies_still_change_identity():
    """posts_count is intentionally not excluded — a reply is a new version of the thread."""
    a = {"id": 1, "posts_count": 4, "views": 10}
    b = {"id": 1, "posts_count": 5, "views": 10}
    assert canonical_hash(a, VOLATILE_FIELDS) != canonical_hash(b, VOLATILE_FIELDS)


def test_prune_handles_dotted_paths_and_missing_keys():
    payload = {"details": {"notification_level": 3, "created_by": "alice"}, "id": 1}
    pruned = prune(payload, ("details.notification_level", "nope.missing", "absent"))
    assert pruned == {"details": {"created_by": "alice"}, "id": 1}
    assert payload["details"]["notification_level"] == 3, "prune must not mutate the input"


def test_prune_traverses_lists_with_wildcard():
    """Discourse read counters live on every post, so the projection must reach into lists."""
    payload = {
        "post_stream": {
            "posts": [
                {"id": 1, "cooked": "<p>hi</p>", "reads": 10, "score": 4.5},
                {"id": 2, "cooked": "<p>yes</p>", "reads": 99, "score": 1.0},
            ]
        }
    }
    pruned = prune(payload, ("post_stream.posts[].reads", "post_stream.posts[].score"))
    assert pruned["post_stream"]["posts"] == [
        {"id": 1, "cooked": "<p>hi</p>"},
        {"id": 2, "cooked": "<p>yes</p>"},
    ]
    assert payload["post_stream"]["posts"][0]["reads"] == 10, "prune must not mutate the input"


def test_post_read_counters_do_not_create_new_versions():
    """A thread nobody edited but many people read must stay one bronze object."""

    def topic(reads, score):
        return {
            "id": 7,
            "post_stream": {
                "posts": [
                    {"id": 1, "cooked": "<p>Proposal text</p>", "reads": reads, "score": score}
                ]
            },
        }

    assert canonical_hash(topic(10, 1.0), VOLATILE_FIELDS) == canonical_hash(
        topic(4210, 88.5), VOLATILE_FIELDS
    )


def test_edited_post_body_still_creates_a_new_version():
    """The counterpart guard: the projection must not hide real edits."""

    def topic(body):
        return {"id": 7, "post_stream": {"posts": [{"id": 1, "cooked": body, "reads": 10}]}}

    assert canonical_hash(topic("<p>v1</p>"), VOLATILE_FIELDS) != canonical_hash(
        topic("<p>v2</p>"), VOLATILE_FIELDS
    )


def test_a_new_reply_still_creates_a_new_version():
    base = {"id": 7, "posts_count": 1, "post_stream": {"posts": [{"id": 1, "cooked": "a"}]}}
    replied = {
        "id": 7,
        "posts_count": 2,
        "post_stream": {"posts": [{"id": 1, "cooked": "a"}, {"id": 2, "cooked": "b"}]},
    }
    assert canonical_hash(base, VOLATILE_FIELDS) != canonical_hash(replied, VOLATILE_FIELDS)
