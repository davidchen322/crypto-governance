"""FastAPI delivery layer over the Phase 5 agent graph and the Phase 3 lakehouse.

Thin by design: every router mounted here formats what an already-built, already-measured
layer returns — `ai_agent.graph` for chat, Trino/silver for proposals. No auth anywhere in
this file. That is the plan's guest-tier default, kept deliberately so `/docs` and a live
query need no key; real multi-tenant auth is Phase 12 and lives entirely in
`backend_api/saas_modules/`, imported from nowhere in this package.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend_api.routes import chat, health, proposals

app = FastAPI(
    title="Crypto Governance Intelligence API",
    description=(
        "Governance monitoring and RAG over Snapshot, protocol forums, and (from Phase 9) "
        "on-chain contract source."
    ),
    version="0.1.0",
)

# Phase 8's dashboard is a browser app calling this API directly (no server-side proxy —
# there is nothing yet for one to broker, per the no-auth stance above). Origins are
# env-configurable so a later deployment doesn't need a code change, comma-separated,
# defaulting to the Next.js dev server.
_origins = os.getenv("DASHBOARD_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(proposals.router)
