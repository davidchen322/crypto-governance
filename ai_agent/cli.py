"""`gov` — command-line access to the platform.

The Phase 4 exit demo:

    gov search "delegate voting power concentration"

Deliberately thin. It formats what `search()` returns and nothing else — no re-ranking, no
filtering of its own, no synthesis. If the demo output looks wrong, the retrieval layer is
wrong, and that is the point of having a demo that reaches the real corpus.

Synthesis arrives in Phase 5 with the router. This prints evidence, not answers.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from ai_agent.chains.retrieval import (
    DEFAULT_MAX_DISTANCE,
    DEFAULT_MIN_GAP,
    SearchResult,
    search,
)
from config.protocols import BY_NAME

# Only used when stdout is a TTY, so piping to a file or another process stays clean.
BOLD, DIM, CYAN, GREEN, YELLOW, RESET = (
    "\033[1m",
    "\033[0;90m",
    "\033[0;36m",
    "\033[0;32m",
    "\033[0;33m",
    "\033[0m",
)


def _plain() -> None:
    global BOLD, DIM, CYAN, GREEN, YELLOW, RESET
    BOLD = DIM = CYAN = GREEN = YELLOW = RESET = ""


def _fmt_date(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "unknown date"


def _snippet(text: str, width: int, lines: int) -> list[str]:
    """Collapse a chunk to a few readable lines.

    The stored text carries real paragraph breaks — Phase 3 was fixed specifically so it
    would — but a terminal listing wants density, so they are flattened here at DISPLAY time
    only. Nothing about the stored chunk changes.
    """
    words, out, current = text.split(), [], ""
    truncated = False
    for i, word in enumerate(words):
        if len(current) + len(word) + 1 > width:
            out.append(current)
            current = word
            if len(out) == lines:
                # Anything left after the final line is what makes this a truncation.
                truncated = i < len(words) - 1
                break
        else:
            current = f"{current} {word}".strip()
    if len(out) < lines and current:
        out.append(current)
    if truncated and out:
        # Tracked explicitly rather than inferred by comparing lengths: the earlier version
        # compared `len(" ".join(words))` against the summed line lengths, which loses one
        # space per line break and so marked EVERY multi-line text as truncated. Harmless in
        # a search listing, actively misleading on an answer — it implies the analyst said
        # more than is shown.
        out[-1] = out[-1].rstrip(" .,") + " ..."
    return out


def _render(results: list[SearchResult], query: str, width: int) -> None:
    print(f'\n{BOLD}"{query}"{RESET}  —  {len(results)} result(s)\n')
    for rank, r in enumerate(results, 1):
        label = BY_NAME[r.protocol_name].label if r.protocol_name in BY_NAME else r.protocol_name
        kind = "proposal" if r.source == "proposal" else "forum"
        title = r.title or "(untitled)"

        print(
            f"{BOLD}{rank}.{RESET} {CYAN}{label}{RESET}  {GREEN}{kind}{RESET}  "
            f"{YELLOW}{_fmt_date(r.document_date)}{RESET}  "
            f"{DIM}distance {r.distance:.3f}{RESET}"
        )
        print(f"   {BOLD}{title[:width]}{RESET}")
        if r.heading:
            print(f"   {DIM}section: {r.heading[:width]}{RESET}")
        for line in _snippet(r.text, width, lines=3):
            print(f"   {DIM}{line}{RESET}")
        print(f"   {DIM}{r.source}:{r.document_id}  chunk {r.chunk_index}{RESET}\n")


def cmd_search(args: argparse.Namespace) -> int:
    results = search(
        args.query,
        k=args.k,
        protocol=args.protocol,
        source=args.source,
        max_distance=None if args.no_threshold else args.max_distance,
        min_gap=None if args.no_threshold else args.min_gap,
        as_of=args.as_of,
    )

    if args.json:
        print(
            json.dumps(
                [
                    {
                        **{
                            f: getattr(r, f)
                            for f in (
                                "source",
                                "document_id",
                                "protocol_name",
                                "chunk_index",
                                "heading",
                                "text",
                                "title",
                            )
                        },
                        "distance": round(r.distance, 4),
                        "document_date": r.document_date.isoformat() if r.document_date else None,
                    }
                    for r in results
                ],
                indent=2,
            )
        )
        return 0

    if not results:
        # Not an error. An empty result is the two-signal threshold doing its job, and
        # saying so is more useful than printing five confident-looking irrelevant chunks.
        print(
            f'\n{BOLD}"{args.query}"{RESET}  —  {YELLOW}no sufficiently relevant chunks{RESET}\n'
            f"  {DIM}The corpus has nothing close enough to answer this. Re-run with\n"
            f"  --no-threshold to see the nearest matches and their distances.{RESET}\n"
        )
        return 0

    _render(results, args.query, args.width)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Phase 5: route, retrieve, judge, synthesise, cite."""
    from ai_agent.graph.llm import usage
    from ai_agent.graph.workflow import default_graph

    state = default_graph().invoke({"question": args.question, "messages": []})

    if args.json:
        print(
            json.dumps(
                {
                    "question": args.question,
                    "route": state.get("route"),
                    "scope": state.get("scope"),
                    "filters": state.get("filters"),
                    "answer": state.get("answer"),
                    "data_gap": state.get("data_gap"),
                    "citations": state.get("citations"),
                    "retrieval_note": state.get("retrieval_note"),
                },
                indent=2,
            )
        )
        return 0

    route = state.get("route", "?")
    scope = state.get("scope", "?")
    filters = {k: v for k, v in (state.get("filters") or {}).items() if v}
    head = f"{CYAN}{route}{RESET}/{scope}" + (f"  {DIM}{filters}{RESET}" if filters else "")
    print(f'\n{BOLD}"{args.question}"{RESET}\n  {DIM}route:{RESET} {head}\n')

    answer = state.get("answer", "")
    for line in _snippet(answer, args.width, lines=100):
        print(f"  {line}")

    cites = state.get("citations") or []
    if cites:
        print(f"\n  {BOLD}sources{RESET}")
        for c in cites:
            print(
                f"   {YELLOW}[{c['marker']}]{RESET} {CYAN}{c['protocol_name']}{RESET} "
                f"{GREEN}{c['source']}{RESET}  {(c.get('title') or '')[:56]}"
            )
            print(f"       {DIM}{c['chunk_ref']}  distance {c['distance']}{RESET}")
    elif not state.get("data_gap"):
        # A structured answer legitimately has no markers — its provenance is the SQL
        # template, which --explain shows. Calling that "no citations" reads as a failure.
        if state.get("rows"):
            print(f"\n  {DIM}answered from structured query results{RESET}")
        else:
            print(f"\n  {YELLOW}no citations resolved{RESET}")

    if args.explain and state.get("retrieval_note"):
        print(f"\n  {DIM}{state['retrieval_note']}{RESET}")
        print(f"  {DIM}llm usage: {usage()}{RESET}")
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gov", description="Crypto governance intelligence.")
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="semantic search over the governance corpus")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=5, help="results to return (default 5)")
    s.add_argument("--protocol", choices=sorted(BY_NAME), help="restrict to one protocol")
    s.add_argument("--source", choices=("proposal", "forum"), help="restrict to one source")
    s.add_argument("--max-distance", type=float, default=DEFAULT_MAX_DISTANCE)
    s.add_argument("--min-gap", type=float, default=DEFAULT_MIN_GAP)
    s.add_argument(
        "--no-threshold",
        action="store_true",
        help="disable both relevance cutoffs and show the raw ranking",
    )
    s.add_argument(
        "--as-of",
        type=datetime.fromisoformat,
        help="ask the question against a past state (ISO 8601)",
    )
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--width", type=int, default=88)
    s.set_defaults(func=cmd_search)

    a = sub.add_parser("ask", help="answer a governance question with citations")
    a.add_argument("question")
    a.add_argument("--json", action="store_true", help="machine-readable output")
    a.add_argument("--explain", action="store_true", help="show routing and retrieval detail")
    a.add_argument("--width", type=int, default=88)
    a.set_defaults(func=cmd_ask)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.json or not sys.stdout.isatty():
        _plain()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
