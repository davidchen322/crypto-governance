"""Discourse forum client.

Every protocol runs its own Discourse instance with its own rate limits and its own terms of
use — there is no single "Discourse API" to be a good citizen of, so the throttle is
per-host and conservative by default.

Discourse keeps native revision history for edited posts, so post edits are recoverable from
the source rather than inferred by diffing snapshots.
"""

from __future__ import annotations

import logging

from data_pipeline.extraction.http import HttpClient

log = logging.getLogger(__name__)

# Reader/author telemetry that moves without the topic being edited. Every entry below was
# identified by diffing stored versions of the same topics, not guessed.
#
# Topic level, from two harvests of 75 topics two minutes apart: `suggested_topics` churned
# 904 times (it is a randomised recommendation sidebar), `related_topics` 42, `views` 4,
# `timeline_lookup` 3, `message_bus_last_id` 1.
#
# Post level, from a later diff restricted to same-scheme versions: `reads` 32,
# `readers_count` 32, `score` 22, `posts_count` 20 (the author's forum-wide tally, which
# moves when they post in any other thread), `incoming_link_count` 6, `trust_level` 1.
#
# Deliberately NOT excluded — these move only on real activity and should produce a new
# bronze version: topic-level posts_count, last_posted_at, like_count, word_count,
# participant_count, and post-level reactions, actions_summary and link_counts.
VOLATILE_FIELDS = (
    # topic level
    "suggested_topics",
    "related_topics",
    "views",
    "timeline_lookup",
    "message_bus_last_id",
    "details.notification_level",
    # post level — `[]` traverses every post in the thread
    "post_stream.posts[].reads",
    "post_stream.posts[].readers_count",
    "post_stream.posts[].score",
    "post_stream.posts[].posts_count",
    "post_stream.posts[].incoming_link_count",
    "post_stream.posts[].trust_level",
)


class DiscourseClient:
    def __init__(self, host: str, http: HttpClient):
        self.host = host.rstrip("/").replace("https://", "").replace("http://", "")
        self.http = http

    @property
    def base(self) -> str:
        return f"https://{self.host}"

    def latest_topics(self, page: int = 0) -> list[dict]:
        data = self.http.request_json("GET", f"{self.base}/latest.json", params={"page": page})
        return (data.get("topic_list") or {}).get("topics") or []

    def topic(self, topic_id: int) -> dict:
        """Full topic including the first page of posts."""
        return self.http.request_json("GET", f"{self.base}/t/{topic_id}.json")

    def fetch_topics(self, limit: int = 30, max_pages: int = 10) -> list[dict]:
        """Topic listings, newest activity first.

        Returns listing metadata only. Post bodies come from `topic()`, which costs one
        request each — kept separate so a caller can decide how deep to go.
        """
        collected: list[dict] = []
        seen: set[int] = set()

        for page in range(max_pages):
            if len(collected) >= limit:
                break
            batch = self.latest_topics(page=page)
            if not batch:
                break
            fresh = [t for t in batch if t["id"] not in seen]
            if not fresh:
                break
            seen.update(t["id"] for t in fresh)
            collected.extend(fresh)

        log.info("discourse: %s topics from %s", len(collected[:limit]), self.host)
        return collected[:limit]
