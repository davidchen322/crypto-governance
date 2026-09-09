"""Pydantic models for the API surface — this is what renders in the `/docs` schema.

Deliberately loose in two places: `filters` (whatever the router extracted) and the
absence of a `rows` model on `ChatResponse`. Modelling every SQL template's column set here
would recreate `ai_agent/graph/sql_templates.py` in a second location that could drift from
it — the CLI's `--json` mode has the same shape for the same reason.
"""

from __future__ import annotations

from pydantic import BaseModel


class ChatRequest(BaseModel):
    question: str


class Citation(BaseModel):
    marker: str
    chunk_ref: str
    source: str
    document_id: str
    protocol_name: str
    title: str | None = None
    distance: float


class ChatResponse(BaseModel):
    question: str
    route: str | None = None
    scope: str | None = None
    filters: dict | None = None
    answer: str | None = None
    data_gap: bool | None = None
    citations: list[Citation] = []
    retrieval_note: str | None = None


class ProposalSummary(BaseModel):
    proposal_id: str
    protocol_name: str
    title: str | None = None
    proposal_state: str | None = None
    proposal_created: str | None = None
    vote_count: int | None = None


class ProposalDetail(BaseModel):
    proposal_id: str
    protocol_name: str
    title: str | None = None
    body: str | None = None
    proposal_state: str | None = None
    author: str | None = None
    voting_start: str | None = None
    voting_end: str | None = None
    vote_count: int | None = None
    scores_total: float | None = None
    proposal_created: str | None = None
    quorum: float | None = None
    choices: list[str] | None = None
    scores: list[float] | None = None
    discussion_url: str | None = None


class ProposalVersion(BaseModel):
    """One SCD2 row from `proposal_versions` — used only by `/history`, which returns every
    version rather than just the current one `ProposalDetail` reads."""

    proposal_id: str
    protocol_name: str
    title: str | None = None
    proposal_state: str | None = None
    vote_count: int | None = None
    scores_total: float | None = None
    content_hash: str
    valid_from: str
    valid_to: str | None = None
    is_current: bool


class HealthComponent(BaseModel):
    status: str  # "ok" | "error"
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    postgres: HealthComponent
    trino: HealthComponent
