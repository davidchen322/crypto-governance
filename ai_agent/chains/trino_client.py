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

import os
import time
from typing import Any

import requests

TRINO_URL = os.getenv("TRINO_URL", "http://localhost:8090")
TRINO_USER = os.getenv("TRINO_USER", "crypto-gov")


class TrinoError(RuntimeError):
    pass


def query(sql: str, url: str = TRINO_URL, user: str = TRINO_USER) -> list[dict[str, Any]]:
    """Run a query and return all rows as dicts.

    Trino streams results across many URIs; a caller that reads only the first response
    silently gets a partial result set, which is the classic way to lose rows here.
    """
    # A username is mandatory even with authentication disabled — without it Trino
    # returns 401 "Basic authentication or X-Trino-Original-User or X-Trino-User must
    # be sent". It is an identity label, not a credential.
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
