#!/usr/bin/env python3
"""Score the intent router against the routing eval set.

    make eval-routing                  # accuracy, the headline number
    make eval-routing ARGS=--verbose   # per-question detail

Two numbers, reported separately on purpose:

  route accuracy   did it pick the right store?   Exit criterion: >= 80%
  scope accuracy   did it pick the right width?   Only meaningful for vector/hybrid

They are separate because they fail differently. A wrong ROUTE sends the question to a store
that cannot answer it. A wrong SCOPE reaches the right store and then retrieves the wrong
shape — measured in Phase 4 as costing documents in both directions, so it is a real error
and not a tuning preference.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_agent.graph.llm import ROUTER_MODEL, usage  # noqa: E402
from ai_agent.graph.router import decide  # noqa: E402

QUESTIONS = Path(__file__).parent / "routing.yaml"
RESULTS = Path(__file__).parent / "routing_baseline.json"

BOLD, DIM, GREEN, RED, RESET = "\033[1m", "\033[0;90m", "\033[0;32m", "\033[0;31m", "\033[0m"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score the router")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--save", action="store_true", help=f"write {RESULTS.name}")
    args = ap.parse_args(argv)

    questions = yaml.safe_load(QUESTIONS.read_text())
    scored = []
    for q in questions:
        got = decide(q["question"])
        route_ok = got["route"] == q["route"]
        scope_ok = ("scope" not in q) or (got["scope"] == q["scope"])
        scored.append(
            {
                "id": q["id"],
                "expected_route": q["route"],
                "got_route": got["route"],
                "route_ok": route_ok,
                "expected_scope": q.get("scope"),
                "got_scope": got["scope"],
                "scope_ok": scope_ok,
                "protocol": got["protocol"],
                "since": got["since"],
                "reason": got["reason"],
            }
        )

    if args.verbose:
        for s in scored:
            mark = f"{GREEN}pass{RESET}" if s["route_ok"] else f"{RED}fail{RESET}"
            detail = f"{s['expected_route']} -> {s['got_route']}"
            if s["expected_scope"]:
                flag = (
                    ""
                    if s["scope_ok"]
                    else f"  {RED}scope {s['expected_scope']}->{s['got_scope']}{RESET}"
                )
                detail += f"  scope {s['got_scope']}{flag}"
            extra = []
            if s["protocol"]:
                extra.append(f"protocol={s['protocol']}")
            if s["since"]:
                extra.append(f"since={s['since']}")
            print(f"  {mark}  {s['id']:<32} {detail}  {DIM}{' '.join(extra)}{RESET}")
        print()

    route_acc = sum(s["route_ok"] for s in scored) / len(scored)
    scoped = [s for s in scored if s["expected_scope"]]
    scope_acc = sum(s["scope_ok"] for s in scoped) / len(scoped) if scoped else 1.0

    print(f"{BOLD}routing{RESET}  ({len(scored)} questions, model {ROUTER_MODEL})")
    print(f"  route accuracy   {route_acc:.2f}   (exit criterion: >= 0.80)")
    print(f"  scope accuracy   {scope_acc:.2f}   ({len(scoped)} vector/hybrid questions)")

    by_route: dict[str, list[bool]] = {}
    for s in scored:
        by_route.setdefault(s["expected_route"], []).append(s["route_ok"])
    for route, oks in sorted(by_route.items()):
        print(f"  {route:<8} {sum(oks)}/{len(oks)}")

    wrong = [s for s in scored if not s["route_ok"]]
    if wrong:
        print(f"\n{BOLD}misrouted{RESET}")
        for s in wrong:
            print(
                f"  {s['id']}: expected {s['expected_route']}, "
                f"got {s['got_route']}  ({s['reason']})"
            )

    u = usage()
    print(f"\n  {u['calls']} calls, {u['tokens']:,} tokens")

    if args.save:
        RESULTS.write_text(
            json.dumps(
                {
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "router_model": ROUTER_MODEL,
                    "route_accuracy": round(route_acc, 4),
                    "scope_accuracy": round(scope_acc, 4),
                    "questions": scored,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"  baseline written to {RESULTS.relative_to(Path.cwd())}")
    return 0 if route_acc >= 0.80 else 1


if __name__ == "__main__":
    sys.exit(main())
