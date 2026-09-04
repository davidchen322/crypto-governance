#!/usr/bin/env python3
"""Print the eval questions with links to their original sources.

    make eval-sources              # everything
    make eval-sources ARGS=lookup  # one kind

Reviewing labels without reading the source documents is guessing. This resolves every
`expect` entry in tests/eval/questions.yaml to a real URL — Snapshot for proposals, the
protocol's own Discourse for forum threads — so a label can be checked against what the
document actually says rather than against its title.

Queries Trino through the container CLI rather than a client library: one fewer
dependency, and it fails loudly if the stack is down instead of hanging on a socket.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
QUESTIONS = REPO / "tests/eval/questions.yaml"

BOLD, DIM, CYAN, GREEN, YELLOW, RESET = (
    "\033[1m",
    "\033[0;90m",
    "\033[0;36m",
    "\033[0;32m",
    "\033[0;33m",
    "\033[0m",
)
KIND_COLOUR = {"lookup": GREEN, "thematic": CYAN, "cross_source": YELLOW, "negative": DIM}


def trino(sql: str) -> list[list[str]]:
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "trino",
            "trino",
            "--output-format=TSV",
            "--execute",
            sql,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        print(
            f"Trino query failed — is the stack up? (`make up`)\n{proc.stderr[-500:]}",
            file=sys.stderr,
        )
        sys.exit(1)
    return [line.split("\t") for line in proc.stdout.strip().splitlines() if line.strip()]


def load_proposal_index() -> dict[str, dict]:
    rows = trino("""
        SELECT proposal_id, protocol_name, title, link, discussion_url
        FROM iceberg.silver.proposal_versions WHERE is_current
    """)
    return {
        r[0]: {"protocol": r[1], "title": r[2], "link": r[3], "discussion": r[4]}
        for r in rows
        if len(r) >= 5
    }


def load_forum_index() -> dict[tuple[str, int], dict]:
    """Keyed by (protocol, topic_id), not topic_id alone — Discourse topic_ids are only
    unique within one forum installation. Keying on topic_id alone silently let one
    protocol's topic overwrite another's in this dict whenever two forums happened to
    reuse the same number (confirmed real in this corpus: topic 7 exists on Arbitrum's,
    Uniswap's, and Optimism's forums as three unrelated threads)."""
    rows = trino("""
        SELECT DISTINCT topic_id, forum_host, topic_slug, topic_title, protocol_name
        FROM iceberg.silver.forum_posts WHERE is_current
    """)
    index = {}
    for r in rows:
        if len(r) < 5:
            continue
        index[(r[4], int(r[0]))] = {
            "url": f"https://{r[1]}/t/{r[2]}/{r[0]}",
            "title": r[3],
            "protocol": r[4],
        }
    return index


def main(argv: list[str]) -> int:
    wanted = argv[1] if len(argv) > 1 else None

    questions = yaml.safe_load(QUESTIONS.read_text())
    proposals = load_proposal_index()
    forums = load_forum_index()

    shown = missing = 0
    for q in questions:
        if wanted and q["kind"] != wanted:
            continue
        shown += 1
        colour = KIND_COLOUR.get(q["kind"], "")
        print(f"\n{BOLD}{q['id']}{RESET}  {colour}[{q['kind']}]{RESET}")
        print(f"  {q['question']}")

        if not q["expect"]:
            print(f"  {DIM}expect: nothing — the corpus cannot answer this{RESET}")
            continue

        for item in q["expect"]:
            if item["source"] == "proposal":
                meta = proposals.get(item["id"])
                if not meta:
                    # A label pointing at a document that is not in silver is a broken
                    # label, not a retrieval failure — surface it now, not at scoring time.
                    print(f"  {YELLOW}! NOT IN SILVER: {item['id'][:20]}…{RESET}")
                    missing += 1
                    continue
                print(f"  · [{meta['protocol']}] {meta['title'][:64]}")
                print(f"    {DIM}{meta['link']}{RESET}")
                if meta["discussion"]:
                    print(f"    {DIM}forum: {meta['discussion']}{RESET}")
            else:
                meta = forums.get((item["protocol"], int(item["topic_id"])))
                if not meta:
                    print(
                        f"  {YELLOW}! NOT IN SILVER: {item['protocol']} topic "
                        f"{item['topic_id']}{RESET}"
                    )
                    missing += 1
                    continue
                print(f"  · [{meta['protocol']}] {meta['title'][:64]}")
                print(f"    {DIM}{meta['url']}{RESET}")

    print(f"\n{shown} question(s) shown.", end="")
    print(
        f" {YELLOW}{missing} label(s) point at documents not in silver.{RESET}"
        if missing
        else " All labels resolve."
    )
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
