"""Minimal Trino reader over its REST API.

Why not the CLI: `--output-format=TSV` was fine while forum text was a single flat line,
but Phase 3 now (correctly) preserves paragraph breaks, so bodies contain literal newlines
and tabs. Parsing that back out of TSV is guesswork. The REST API returns typed JSON and
sidesteps the question.

Why not a client library: `requests` is already a dependency and the protocol is a POST
followed by a `nextUri` chase. Roughly thirty lines against another package to install,
pin and keep current.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

TRINO_URL = os.getenv("TRINO_URL", "http://localhost:8090")
TRINO_USER = os.getenv("TRINO_USER", "crypto-gov")

# Found by running the real integration suite repeatedly, not by inspection: under the CPU
# load of a long pytest session (many other tests running concurrently-ish elsewhere in the
# same process), Trino intermittently reports "was abandoned by the client, as it may have
# exited or stopped checking for query results" for a query that never actually stopped
# being polled — Trino's own abandonment timeout fired before this process's next poll made
# it through, not because anything was wrong with the query. Measured: reproduced twice in
# roughly a dozen full-suite runs, always this error, never in isolation. A query's
# server-side state is gone once abandoned — resuming mid-pagination isn't possible — so
# the whole query is retried fresh, matching the retry-on-transient-condition pattern
# `data_pipeline/extraction/http.py` already uses for Snapshot and Discourse.
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 0.5


class TrinoError(RuntimeError):
    pass


def _run_once(sql: str, url: str, user: str) -> list[dict[str, Any]]:
    """A username is mandatory even with authentication disabled — without it Trino
    returns 401 "Basic authentication or X-Trino-Original-User or X-Trino-User must
    be sent". It is an identity label, not a credential."""
    headers = {"X-Trino-User": user}
    response = requests.post(f"{url}/v1/statement", data=sql.encode(), headers=headers, timeout=60)
    response.raise_for_status()
    payload = response.json()

    columns: list[str] = []
    rows: list[dict[str, Any]] = []

    while True:
        if payload.get("error"):
            raise TrinoError(payload["error"].get("message", str(payload["error"])))
        if payload.get("columns") and not columns:
            columns = [c["name"] for c in payload["columns"]]
        for row in payload.get("data") or []:
            rows.append(dict(zip(columns, row, strict=False)))

        next_uri = payload.get("nextUri")
        if not next_uri:
            return rows
        # Trino asks clients to pace themselves while a query is still planning.
        time.sleep(0.05)
        follow = requests.get(next_uri, headers=headers, timeout=60)
        follow.raise_for_status()
        payload = follow.json()


def query(sql: str, url: str = TRINO_URL, user: str = TRINO_USER) -> list[dict[str, Any]]:
    """Run a query and return all rows as dicts.

    Trino streams results across many URIs; a caller that reads only the first response
    silently gets a partial result set, which is the classic way to lose rows here.

    Retries the whole query — not just the next page — specifically on "abandoned by the
    client" (see MAX_ATTEMPTS's comment for why this one condition is safe to retry and
    others are not: a genuine SQL error would just fail identically again, so only this
    specific, measured-transient condition gets a second attempt).
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            return _run_once(sql, url, user)
        except TrinoError as exc:
            # Not the one transient condition this retries, or no attempts left either
            # way: propagate rather than mask a genuine error behind a retry loop.
            if "abandoned" not in str(exc).lower() or attempt == MAX_ATTEMPTS - 1:
                raise
            log.warning(
                "Trino query abandoned (attempt %d/%d), retrying: %s",
                attempt + 1,
                MAX_ATTEMPTS,
                exc,
            )
            time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
    raise AssertionError("unreachable: the loop above always returns or raises")
