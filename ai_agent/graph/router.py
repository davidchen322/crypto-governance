"""The intent router.

Reads the question and nothing else. It never touches the corpus — a router that had to
query data in order to decide how to query data would be circular and expensive, and it is
the reason routing can run on a small cheap model while only synthesis needs a strong one.

It emits two things, not one:

  route   which store can answer this      sql | vector | hybrid
  scope   how wide the search should be    one | all

`scope` is the addition Phase 4's measurements forced. The three routes answer *which store*
and say nothing about *how wide*, and for thematic questions the width matters more:

  * a cross-protocol question ("which protocols pay delegates?") needs every protocol
    examined — measured, fan-out finds documents a single ranked list never surfaces at any k
  * a single-protocol question ("what is Aave doing about risk?") is DAMAGED by that same
    treatment: 4/4 -> 3/4 on two eval questions, because slots get reserved for protocols
    with nothing to say

So getting scope wrong is not a missed optimisation, it is an active regression in both
directions — which is why it is part of the routing contract and part of the eval set.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ai_agent.graph.llm import ROUTER_MODEL, complete_json
from ai_agent.graph.state import AgentState
from config.protocols import BY_NAME

VALID_ROUTES = ("sql", "vector", "hybrid")
VALID_SCOPES = ("one", "all")

PROMPT = """You route governance questions to the data store that can answer them.

STORES
- sql: structured columns only. Counts, groupings, orderings, date bounds, status filters.
  Available fields: protocol_name, proposal_state (active/closed), proposal_created,
  voting_start, voting_end, vote_count, scores_total, title, author; and forum post counts.
- vector: the MEANING of prose. Reasons, arguments, definitions, themes, "what did people
  say". No column holds these.
- hybrid: needs BOTH a structured constraint AND a judgement about prose.

DECIDING
Ask: can columns alone answer this exactly?
  yes                                  -> sql
  no, and no structured constraint     -> vector
  no, but there IS a structured constraint (a date, a status, a named protocol) -> hybrid

A question that LOOKS like a count but needs meaning to decide what to count is hybrid.
"How many proposals mention risk parameters?" is hybrid, not sql: no column holds "mentions
risk parameters".

SCOPE (for vector and hybrid only)
- "one": the question is about a single protocol, named or clearly implied.
- "all": the question ranges across protocols. Anything of the form "which protocols...",
  "what are DAOs doing...", "how do different DAOs..." is "all" — naming the protocols with
  NOTHING to say is part of a correct answer to those.

FILTERS — extract only what the question states explicitly. Never guess.
- protocol: one of {protocols}, or null
- since: ISO date (YYYY-MM-DD) if the question bounds time, else null.
  TODAY IS {today}. Resolve relative expressions against that date — "the last quarter"
  means roughly three months before today, "this year" means January 1 of the current year.
  Without an anchor a model invents a plausible-looking year; measured, "the last quarter"
  resolved to 2023-07-01 on a 2026 corpus.
- source: "proposal" or "forum" ONLY if the question asks exclusively for one of them.
  If it mentions both, or implies both ("...and what did the forum argue about them?"),
  this MUST be null — setting it excludes the other half of the evidence outright.

QUESTION: {question}

Return ONLY JSON:
{{"route":"sql|vector|hybrid","scope":"one|all","protocol":null,"since":null,
  "source":null,"reason":"<12 words"}}"""


def decide(question: str) -> dict[str, Any]:
    """Route one question. Pure — no state, so the eval harness can call it directly."""
    raw = complete_json(
        PROMPT.format(
            question=question,
            protocols=", ".join(sorted(BY_NAME)),
            today=date.today().isoformat(),
        ),
        model=ROUTER_MODEL,
    )
    return normalise(raw)


def normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a model's JSON into something the graph can rely on.

    Every field is validated rather than trusted. A model that returns "SQL", "vector search"
    or a protocol we do not cover should degrade to a sane default, not propagate a value
    that fails somewhere deeper where the cause is no longer visible.
    """
    route = str(raw.get("route", "")).strip().lower()
    if route not in VALID_ROUTES:
        route = "vector"

    scope = str(raw.get("scope", "")).strip().lower()
    if scope not in VALID_SCOPES:
        # Defaulting to "all" is the safe direction: too wide costs context, too narrow can
        # exclude the answer outright.
        scope = "all"

    protocol = raw.get("protocol")
    protocol = protocol if isinstance(protocol, str) and protocol.lower() in BY_NAME else None
    if protocol:
        protocol = protocol.lower()

    source = raw.get("source")
    source = source if source in ("proposal", "forum") else None

    since = raw.get("since")
    since = since if isinstance(since, str) and len(since) == 10 and since[4] == "-" else None

    # A question naming one protocol is about that protocol, whatever the model said about
    # scope. This is a cheap consistency repair, not second-guessing.
    if protocol and scope == "all" and route != "sql":
        scope = "one"

    return {
        "route": route,
        "scope": scope,
        "protocol": protocol,
        "since": since,
        "source": source,
        "reason": str(raw.get("reason", ""))[:120],
    }


def router_node(state: AgentState) -> dict[str, Any]:
    d = decide(state["question"])
    return {
        "route": d["route"],
        "scope": d["scope"],
        "filters": {"protocol": d["protocol"], "since": d["since"], "source": d["source"]},
    }
