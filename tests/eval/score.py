#!/usr/bin/env python3
"""Score retrieval against the reviewed eval set.

    make eval                 # recall@5, the headline number
    make eval ARGS="--k 10"   # sweep k
    make eval ARGS=--verbose  # per-question detail

Recall is measured at the DOCUMENT level, not the chunk level: a question is satisfied for
a given expected document if *any* chunk of it appears in the top k. Chunk-level scoring
would punish a retriever for returning the right proposal's Summary when the label happened
to be written against its Specification, which is not a real failure.

Positives and negatives are scored differently, on purpose:

  * Positive questions score recall — of the documents that should have come back, how many
    did. The distance threshold is DISABLED here, because recall measures the ranking, not
    the cutoff.
  * Negative questions score the threshold — with it ENABLED, did anything survive? The
    corpus cannot answer them, so any surviving chunk is a false positive that the analyst
    would go on to write a confident answer from.

Reporting them as one blended number would let a loose threshold hide inside good recall.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_agent.chains.retrieval import (  # noqa: E402
    DEFAULT_MAX_DISTANCE,
    DEFAULT_MIN_GAP,
    SearchResult,
    search,
)

QUESTIONS = Path(__file__).parent / "questions.yaml"
RESULTS = Path(__file__).parent / "baseline.json"

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m",
    "\033[0;90m",
    "\033[0;32m",
    "\033[0;31m",
    "\033[0;33m",
    "\033[0m",
)


def expected_key(item: dict) -> tuple[str, str]:
    """Normalise a label to (source, document_id) — the shape retrieval returns."""
    if item["source"] == "proposal":
        return ("proposal", str(item["id"]))
    return ("forum", str(item["topic_id"]))


def retrieved_keys(results: list[SearchResult]) -> set[tuple[str, str]]:
    return {(r.source, r.document_id) for r in results}


def score_question(q: dict, k: int, threshold: float, min_gap: float) -> dict:
    if q["kind"] == "negative":
        # Cutoffs ON: the question is whether anything survives them.
        hits = search(q["question"], k=k, max_distance=threshold, min_gap=min_gap)
        return {
            "id": q["id"],
            "kind": q["kind"],
            "passed": len(hits) == 0,
            "leaked": len(hits),
            "closest": round(min((h.distance for h in hits), default=float("nan")), 4)
            if hits
            else None,
        }

    # Cutoffs OFF: recall measures the ranking and the diversity policy, not the cutoff.
    results = search(q["question"], k=k, max_distance=None, min_gap=None)
    found = retrieved_keys(results)
    expected = {expected_key(e) for e in q["expect"]}
    matched = expected & found
    return {
        "id": q["id"],
        "kind": q["kind"],
        "expected": len(expected),
        "matched": len(matched),
        "recall": len(matched) / len(expected) if expected else 0.0,
        "passed": matched == expected,
        "missed": sorted(f"{s}:{d[:14]}" for s, d in expected - matched),
        "best_distance": round(min((r.distance for r in results), default=float("nan")), 4)
        if results
        else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score retrieval against the eval set")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=DEFAULT_MAX_DISTANCE)
    parser.add_argument("--min-gap", type=float, default=DEFAULT_MIN_GAP)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--save", action="store_true", help=f"write {RESULTS.name}")
    args = parser.parse_args(argv)

    questions = yaml.safe_load(QUESTIONS.read_text())
    scored = [score_question(q, args.k, args.threshold, args.min_gap) for q in questions]

    positives = [s for s in scored if s["kind"] != "negative"]
    negatives = [s for s in scored if s["kind"] == "negative"]

    if args.verbose:
        for s in scored:
            mark = f"{GREEN}pass{RESET}" if s["passed"] else f"{RED}fail{RESET}"
            if s["kind"] == "negative":
                detail = (
                    "nothing retrieved"
                    if s["passed"]
                    else f"{s['leaked']} leaked (closest {s['closest']})"
                )
            else:
                detail = f"recall {s['matched']}/{s['expected']}"
                if s["missed"]:
                    detail += f"  missed {', '.join(s['missed'])}"
            print(f"  {mark}  {s['id']:<34} {DIM}{s['kind']:<12}{RESET} {detail}")
        print()

    by_kind: dict[str, list[float]] = {}
    for s in positives:
        by_kind.setdefault(s["kind"], []).append(s["recall"])

    micro = sum(s["matched"] for s in positives) / max(1, sum(s["expected"] for s in positives))
    macro = sum(s["recall"] for s in positives) / max(1, len(positives))
    clean = sum(1 for s in negatives if s["passed"])

    print(f"{BOLD}recall@{args.k}{RESET}  (threshold off — measuring the ranking)")
    for kind, recalls in sorted(by_kind.items()):
        print(f"  {kind:<14} {sum(recalls) / len(recalls):.2f}   ({len(recalls)} questions)")
    print(f"  {BOLD}micro{RESET}          {micro:.2f}   (documents found / documents expected)")
    print(f"  {BOLD}macro{RESET}          {macro:.2f}   (mean per-question recall)")

    print(
        f"\n{BOLD}negatives{RESET}  (distance<={args.threshold}, "
        f"gap>={args.min_gap} — measuring the cutoff)"
    )
    print(f"  clean          {clean}/{len(negatives)}")
    for s in negatives:
        if not s["passed"]:
            print(
                f"  {YELLOW}leak{RESET}  {s['id']}: {s['leaked']} chunk(s), closest {s['closest']}"
            )

    if args.save:
        RESULTS.write_text(
            json.dumps(
                {
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "k": args.k,
                    "threshold": args.threshold,
                    "min_gap": args.min_gap,
                    "micro_recall": round(micro, 4),
                    "macro_recall": round(macro, 4),
                    "negatives_clean": clean,
                    "negatives_total": len(negatives),
                    "by_kind": {k: round(sum(v) / len(v), 4) for k, v in by_kind.items()},
                    "questions": scored,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"\nbaseline written to {RESULTS.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
