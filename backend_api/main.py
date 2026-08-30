"""FastAPI delivery layer over the Phase 5 agent graph and the Phase 3 lakehouse.

Thin by design: every router mounted here formats what an already-built, already-measured
layer returns — `ai_agent.graph` for chat, Trino/silver for proposals. No auth anywhere in
this file. That is the plan's guest-tier default, kept deliberately so `/docs` and a live
query need no key; real multi-tenant auth is Phase 12 and lives entirely in
`backend_api/saas_modules/`, imported from nowhere in this package.
"""

from __future__ import annotations

from fastapi import FastAPI

from backend_api.routes import chat, health, proposals

app = FastAPI(
    title="Crypto Governance Intelligence API",
    description=(
        "Governance monitoring and RAG over Snapshot, protocol forums, and (from Phase 9) "
        "on-chain contract source."
    ),
    version="0.1.0",
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(proposals.router)
