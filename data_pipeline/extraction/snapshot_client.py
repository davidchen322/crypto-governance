"""Snapshot GraphQL client.

Endpoint is `https://hub.snapshot.org/graphql` — `https://snapshot.org` is the web app and
returns HTML. The proposal state field is `state` (pending | active | closed); there is no
`status` field, and asking for one fails the whole query.
"""

from __future__ import annotations

import logging

from data_pipeline.extraction.http import HttpClient

log = logging.getLogger(__name__)

ENDPOINT = "https://hub.snapshot.org/graphql"

# `discussion` is the link to the protocol's forum thread and is the join key between
# Snapshot and Discourse. `ipfs` is the content CID — proposal bodies are pinned, so that
# value is a durable identity for the text.
PROPOSAL_FIELDS = """
    id
    ipfs
    title
    body
    discussion
    state
    type
    start
    end
    created
    author
    quorum
    scores
    scores_total
    votes
    choices
    link
    space { id name }
"""

PROPOSALS_QUERY = f"""
query Proposals($space: String!, $first: Int!, $createdLt: Int) {{
  proposals(
    first: $first
    where: {{ space: $space, created_lt: $createdLt }}
    orderBy: "created"
    orderDirection: desc
  ) {{
    {PROPOSAL_FIELDS}
  }}
}}
"""

SPACE_QUERY = """
query Space($id: String!) {
  space(id: $id) { id name about network symbol proposalsCount followersCount }
}
"""


class SnapshotError(RuntimeError):
    pass


class SnapshotClient:
    def __init__(self, http: HttpClient, endpoint: str = ENDPOINT):
        self.http = http
        self.endpoint = endpoint

    def _query(self, query: str, variables: dict) -> dict:
        payload = self.http.request_json(
            "POST",
            self.endpoint,
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json"},
        )
        # GraphQL reports failures in a 200 body, so raise_for_status alone is not enough.
        if payload.get("errors"):
            raise SnapshotError(f"GraphQL errors: {payload['errors']}")
        return payload.get("data") or {}

    def fetch_space(self, space_id: str) -> dict | None:
        return self._query(SPACE_QUERY, {"id": space_id}).get("space")

    def fetch_proposals(self, space_id: str, limit: int = 100, page_size: int = 50) -> list[dict]:
        """Newest first, paginated on a `created_lt` cursor.

        Cursor rather than `skip`: the API caps skip depth, and a cursor stays correct even
        if new proposals arrive mid-pagination.
        """
        collected: list[dict] = []
        cursor: int | None = None
        seen: set[str] = set()

        while len(collected) < limit:
            batch_size = min(page_size, limit - len(collected))
            data = self._query(
                PROPOSALS_QUERY,
                {"space": space_id, "first": batch_size, "createdLt": cursor},
            )
            batch = data.get("proposals") or []
            if not batch:
                break

            fresh = [p for p in batch if p["id"] not in seen]
            seen.update(p["id"] for p in fresh)
            collected.extend(fresh)

            oldest = min(p["created"] for p in batch)
            if cursor is not None and oldest >= cursor:
                # Cursor failed to advance; stop rather than loop forever.
                log.warning("cursor stalled for %s at created=%s", space_id, oldest)
                break
            cursor = oldest

            if len(batch) < batch_size:
                break

        log.info("snapshot: %s proposals from %s", len(collected), space_id)
        return collected[:limit]
