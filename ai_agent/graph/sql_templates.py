"""Parameterised SQL the router's SQL branch is allowed to run.

The model NEVER emits SQL. It picks a template name and fills typed parameters; this module
owns the actual text. That is the difference between a question-answering system and a
prompt-injection target — a template cannot be talked into `DROP TABLE`, and there is no
string interpolation of model output into a query.

Every template constrains `is_current`. Silver is SCD2, so a proposal edited three times has
three rows; without that predicate every count is inflated by edit history, and the number
looks plausible enough that nobody checks it.
"""

from __future__ import annotations

from typing import Any

PROTOCOLS_SQL = "('aave','uniswap','arbitrum','optimism','ens')"

TEMPLATES: dict[str, dict[str, Any]] = {
    "count_proposals": {
        "description": "Count proposals, optionally filtered by protocol and/or state.",
        "params": ["protocol", "state"],
        "sql": """
            SELECT protocol_name, proposal_state, count(*) AS n
            FROM iceberg.silver.proposal_versions
            WHERE is_current
              AND ({protocol} IS NULL OR protocol_name = {protocol})
              AND ({state} IS NULL OR proposal_state = {state})
            GROUP BY 1, 2 ORDER BY 1, 2
        """,
    },
    "count_by_protocol": {
        "description": "How many proposals each protocol has.",
        "params": [],
        "sql": """
            SELECT protocol_name, count(*) AS n
            FROM iceberg.silver.proposal_versions
            WHERE is_current GROUP BY 1 ORDER BY 2 DESC
        """,
    },
    "top_proposals_by": {
        "description": "Highest proposals by vote_count or scores_total.",
        "params": ["field", "limit"],
        "sql": """
            SELECT protocol_name, title, vote_count, scores_total, proposal_created
            FROM iceberg.silver.proposal_versions
            WHERE is_current ORDER BY {field} DESC NULLS LAST LIMIT {limit}
        """,
        "enums": {"field": ("vote_count", "scores_total")},
    },
    "proposal_date_extreme": {
        "description": "Earliest or latest proposal by creation date.",
        "params": ["direction"],
        "sql": """
            SELECT protocol_name, title, proposal_created
            FROM iceberg.silver.proposal_versions
            WHERE is_current AND proposal_created IS NOT NULL
            ORDER BY proposal_created {direction} LIMIT 5
        """,
        "enums": {"direction": ("ASC", "DESC")},
    },
    "forum_volume": {
        "description": "Forum post and topic counts per protocol.",
        "params": [],
        "sql": """
            SELECT protocol_name, count(*) AS posts, count(DISTINCT topic_id) AS topics
            FROM iceberg.silver.forum_posts
            WHERE is_current GROUP BY 1 ORDER BY 2 DESC
        """,
    },
    "list_proposals": {
        "description": "List proposals, optionally by protocol, state and creation date.",
        "params": ["protocol", "state", "since", "limit"],
        "sql": """
            SELECT proposal_id, protocol_name, title, proposal_state, proposal_created,
                   vote_count
            FROM iceberg.silver.proposal_versions
            WHERE is_current
              AND ({protocol} IS NULL OR protocol_name = {protocol})
              AND ({state} IS NULL OR proposal_state = {state})
              AND ({since} IS NULL OR proposal_created >= from_iso8601_timestamp({since}))
            ORDER BY proposal_created DESC LIMIT {limit}
        """,
    },
}


class TemplateError(ValueError):
    pass


def _quote(value: Any) -> str:
    """Render a parameter as a SQL literal. The ONLY place model output reaches a query.

    Trino's REST interface here takes a statement string, so values are rendered rather than
    bound. Every value is therefore either NULL, a validated integer, or a single-quoted
    string with quotes doubled — and strings are additionally constrained by the caller to
    known enums or protocol names.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        raise TemplateError("boolean parameters are not supported")
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if len(text) > 64:
        raise TemplateError(f"parameter too long: {len(text)} chars")
    return "'" + text.replace("'", "''") + "'"


def render(name: str, params: dict[str, Any]) -> str:
    """Build a statement from a named template and validated parameters."""
    if name not in TEMPLATES:
        raise TemplateError(f"unknown template {name!r}; known: {sorted(TEMPLATES)}")
    spec = TEMPLATES[name]

    values: dict[str, str] = {}
    for key in spec["params"]:
        raw = params.get(key)

        enums = spec.get("enums", {}).get(key)
        if enums:
            if raw not in enums:
                raise TemplateError(f"{name}.{key} must be one of {enums}, got {raw!r}")
            values[key] = raw  # enum members are literals in this module, never user text
            continue

        if key == "limit":
            n = int(raw) if raw is not None else 10
            values[key] = str(max(1, min(n, 100)))
            continue

        if key == "protocol" and raw is not None:
            if str(raw).lower() not in ("aave", "uniswap", "arbitrum", "optimism", "ens"):
                raise TemplateError(f"unknown protocol {raw!r}")
            raw = str(raw).lower()

        if key == "state" and raw is not None:
            if str(raw).lower() not in ("active", "closed", "pending"):
                raise TemplateError(f"unknown proposal state {raw!r}")
            raw = str(raw).lower()

        values[key] = _quote(raw)

    sql = spec["sql"].format(**values)
    if "is_current" not in sql:
        # Belt and braces: a future template that forgets this silently double-counts edits.
        raise TemplateError(f"template {name!r} does not constrain is_current")
    return " ".join(sql.split())


def catalogue() -> str:
    """Template list for the model's prompt."""
    return "\n".join(
        f"- {n}({', '.join(s['params']) or 'no params'}): {s['description']}"
        for n, s in TEMPLATES.items()
    )
