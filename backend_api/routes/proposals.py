"""`/proposals` — read-only views over `silver.proposal_versions`, via Trino.

Reuses the SQL templates the Phase 5 SQL node already runs (`ai_agent/graph/sql_templates.py`)
rather than writing a second query surface. That keeps exactly one place that knows how to
query proposal identity safely, `is_current` included, instead of two that could drift.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ai_agent.chains.trino_client import query as trino_query
from ai_agent.graph.sql_templates import TemplateError, render
from backend_api.schemas import ProposalDetail, ProposalSummary, ProposalVersion
from config.protocols import BY_NAME

router = APIRouter(prefix="/proposals", tags=["proposals"])

VALID_STATES = ("active", "closed", "pending")


@router.get("", response_model=list[ProposalSummary])
def list_proposals(
    protocol: str | None = Query(None, description="one of: " + ", ".join(sorted(BY_NAME))),
    state: str | None = Query(None, description="one of: " + ", ".join(VALID_STATES)),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    try:
        sql = render(
            "list_proposals",
            {
                "protocol": protocol,
                "state": state,
                "since": None,
                "limit": limit,
                "offset": offset,
            },
        )
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return trino_query(sql)


@router.get("/{proposal_id}", response_model=ProposalDetail)
def get_proposal(proposal_id: str) -> dict:
    try:
        sql = render("get_proposal", {"proposal_id": proposal_id})
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = trino_query(sql)
    if not rows:
        raise HTTPException(status_code=404, detail=f"no current proposal with id {proposal_id!r}")
    return rows[0]


@router.get("/{proposal_id}/history", response_model=list[ProposalVersion])
def get_proposal_history(proposal_id: str) -> list[dict]:
    """Every known SCD2 version of one proposal, oldest first — what the version-history
    timeline (Phase 8) renders. Unlike `get_proposal`, deliberately not current-only."""
    try:
        sql = render("get_proposal_history", {"proposal_id": proposal_id})
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = trino_query(sql)
    if not rows:
        raise HTTPException(status_code=404, detail=f"no proposal with id {proposal_id!r}")
    return rows
