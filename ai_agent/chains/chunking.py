"""Split governance documents into embeddable chunks.

Structure-aware rather than a blind sliding window. Governance text is heavily sectioned —
proposals open with `## Summary`, then `## Motivation`, `## Specification`, and a reader
looking for the risk parameters wants that section, not a window that happens to straddle
it. Splitting on headings keeps a chunk semantically whole; the token window is the
fallback for sections too long to embed in one piece.

Sizing: `text-embedding-3-large` accepts 8191 tokens, so the cap here is not the model's
limit but retrieval quality. A chunk large enough to contain three unrelated topics
retrieves for all three and answers none of them well; a chunk too small loses the context
that made it meaningful. ~400 tokens with ~80 of overlap is a defensible starting point,
and it is a *starting* point — the eval set exists so this can be tuned against a number
instead of taste.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tiktoken

# text-embedding-3-* share cl100k_base. Counting real tokens rather than estimating from
# characters matters here: the cost, the model's hard limit, and chunk-size consistency
# are all denominated in tokens, and chars/4 drifts badly on code blocks and addresses.
ENCODING = tiktoken.get_encoding("cl100k_base")

# Bumping this re-embeds the corpus. It exists because the loader's idempotency check is
# keyed on the SOURCE content hash, which does not move when the chunking logic changes —
# exactly the trap that let a stale Phase 3 derivation survive a "successful" rebuild. With
# a scheme in the key, changing how text is chunked invalidates the old vectors explicitly
# instead of silently reusing them.
#
#   v1  title + heading + body
#   v2  protocol label prepended, so the embedding can see which DAO a chunk belongs to
CHUNK_SCHEME = "v2"

TARGET_TOKENS = 400
OVERLAP_TOKENS = 80
# Deliberately low. A short-but-real section ("## Motivation\n\nThese feeds are high
# risk.") is a coherent standalone chunk and merging it would produce a chunk covering two
# topics, which retrieves for both and answers neither. Only near-empty fragments merge.
MIN_TOKENS = 12

# Markdown ATX headings, which is what both Snapshot bodies and Discourse posts use.
HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def count_tokens(text: str) -> int:
    return len(ENCODING.encode(text))


@dataclass(frozen=True)
class Chunk:
    text: str
    index: int
    tokens: int
    heading: str | None  # nearest enclosing heading, carried for provenance
    # Tokens of body text, EXCLUDING the protocol/title prefix that chunk_proposal prepends.
    # The prefix is identical across every chunk of a document, so it carries no per-chunk
    # information — and on a very short chunk it dominates the embedding, which is how a
    # 14-token forum reply came to sit near any query naming its protocol. Curation decisions
    # about whether a chunk says anything must therefore use this, not `tokens`.
    body_tokens: int = 0


def _split_by_headings(text: str) -> list[tuple[str | None, str]]:
    """Return (heading, body) sections. Text before the first heading gets heading=None."""
    matches = list(HEADING.finditer(text))
    if not matches:
        return [(None, text)]

    # A "heading" that swallows the whole document is not a heading — it is a single-line
    # document that happens to start with '#'. Splitting on it yields one section with an
    # empty body, which the loop below drops, silently discarding the entire document.
    # This is not hypothetical: a 2,123-character Arbitrum milestone report was lost this
    # way before Phase 3 stopped flattening newlines out of forum text.
    if len(matches) == 1 and not text[matches[0].end() :].strip():
        return [(None, text.lstrip("# ").strip())]

    sections: list[tuple[str | None, str]] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append((None, preamble))

    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if body:
            sections.append((match.group(2).strip(), body))
    return sections


def _window(text: str, target: int, overlap: int) -> list[str]:
    """Token-window fallback for sections too long to keep whole.

    Windows are cut on token boundaries and decoded back to text, so a chunk never splits
    a token — but it can split a sentence. Overlap is what keeps a sentence straddling the
    boundary retrievable from at least one side.
    """
    tokens = ENCODING.encode(text)
    if len(tokens) <= target:
        return [text]

    step = max(1, target - overlap)
    out = []
    for start in range(0, len(tokens), step):
        piece = tokens[start : start + target]
        if not piece:
            break
        out.append(ENCODING.decode(piece).strip())
        if start + target >= len(tokens):
            break
    return [p for p in out if p]


def chunk_document(
    text: str,
    target_tokens: int = TARGET_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
    min_tokens: int = MIN_TOKENS,
) -> list[Chunk]:
    """Split one document into chunks, preserving section structure where possible."""
    if not text or not text.strip():
        return []

    pieces: list[tuple[str | None, str]] = []
    for heading, body in _split_by_headings(text):
        if count_tokens(body) <= target_tokens:
            pieces.append((heading, body))
        else:
            # Prepend the heading to each window so a mid-section chunk still says what
            # section it came from — otherwise a window from deep inside "Specification"
            # reads as context-free text.
            for window in _window(body, target_tokens, overlap_tokens):
                pieces.append((heading, window))

    # Fold runt sections into the previous chunk. A near-empty chunk embeds to a vector
    # that matches almost anything and crowds out real content.
    #
    # The merged section's heading is carried into the text, not discarded. Dropping it
    # loses a real retrieval signal: a query about "motivation for the deprecation" has
    # nothing to match if the word "Motivation" was thrown away with the section boundary.
    merged: list[tuple[str | None, str]] = []
    for heading, body in pieces:
        if merged and count_tokens(body) < min_tokens:
            prev_heading, prev_body = merged[-1]
            carried = f"{heading}\n\n{body}" if heading else body
            merged[-1] = (prev_heading, f"{prev_body}\n\n{carried}")
        else:
            merged.append((heading, body))

    chunks = []
    for index, (heading, body) in enumerate(merged):
        prefixed = f"{heading}\n\n{body}" if heading else body
        tokens = count_tokens(prefixed)
        chunks.append(
            Chunk(
                text=prefixed.strip(),
                index=index,
                tokens=tokens,
                heading=heading,
                # No title prefix has been applied at this level, so body == whole chunk.
                body_tokens=tokens,
            )
        )
    return chunks


def chunk_proposal(title: str | None, body: str | None, protocol: str | None = None) -> list[Chunk]:
    """Proposals lead with protocol and title in every chunk.

    A body chunk about fee parameters is far more findable when the text itself says which
    proposal — and which DAO — it belongs to. The embedding has no access to the row's other
    columns, so `protocol_name` sitting in Postgres does nothing for similarity.

    Measured motivation: an Aave question about "V4" retrieved Uniswap's "Four for V4" as
    its top two hits, because "V4" is shared vocabulary and nothing in the text
    disambiguated it.

    This is deliberately a soft signal rather than a hard `WHERE protocol_name = ...`
    filter. A filter that guesses wrong excludes the right answer outright, and it breaks
    cross-protocol questions ("which DAOs pay delegates?") that legitimately span all five.
    A prefix only nudges the ranking.
    """
    title = (title or "").strip()
    if protocol:
        title = f"{protocol} — {title}" if title else protocol
    body = (body or "").strip()
    if not body:
        # Title-only chunk. `body_tokens=0` is the honest report and it matters downstream:
        # curation needs to distinguish "no body at all" from "a short body", because the
        # former has nothing to retrieve. In silver, 9 forum posts have an empty body, and
        # they all hash to sha256("") — so the UNIQUE constraint collapses every one of them
        # onto a single embedding row carrying an arbitrary one's title.
        return (
            [
                Chunk(text=c.text, index=c.index, tokens=c.tokens, heading=c.heading, body_tokens=0)
                for c in chunk_document(title)
            ]
            if title
            else []
        )

    chunks = chunk_document(body)
    if not title:
        return chunks
    return [
        Chunk(
            text=f"{title}\n\n{c.text}",
            index=c.index,
            tokens=count_tokens(f"{title}\n\n{c.text}"),
            heading=c.heading,
            # Carried through unprefixed: this is what curation filters must judge.
            body_tokens=c.body_tokens,
        )
        for c in chunks
    ]


def chunk_forum_post(
    topic_title: str | None, body_text: str | None, protocol: str | None = None
) -> list[Chunk]:
    """Forum posts carry their thread title and protocol for the same reason."""
    return chunk_proposal(topic_title, body_text, protocol)
