"""What does not belong in the semantic corpus.

Curation policy, kept separate from both extraction and chunking. Bronze stays complete —
nothing here deletes source data — but a chunk that reaches `document_embeddings` is a chunk
retrieval can return and an analyst can be shown, and some source text is actively harmful
there.

Two independent rules, because two different things went wrong.

RULE 1 — platform boilerplate.
Discourse auto-creates a welcome topic and per-category "About the ..." topics on every
install. They are artifacts of the forum software, not governance discussion. Arbitrum's
copy accumulated dozens of one-line replies ("You need $ARB tokens", "Mining,staking,
delegate???") and those chunks became *attractors*: measured, `forum:7` was the top hit for
"how many cats climbed up my tree?", for "delegate voting power concentration", and for
"what's the market cap of the ARB token?" — the last of which it caused to pass both
relevance cutoffs and return a confident, useless citation.

Matched on TITLE, not topic id. Discourse's default topic ids differ per install, so an
id-based denylist would be correct for exactly one forum and silently wrong for the rest.

RULE 2 — chunks with negligible content.
Independent of which thread it came from, a chunk carrying a handful of words cannot answer
anything, and under chunk scheme v2 it is actively misleading: the protocol/title prefix is a
fixed ~8-12 tokens, so on a 14-token chunk the embedding is mostly *protocol name*. It then
sits near any query mentioning that protocol, regardless of subject.

This is a different constant from `MIN_TOKENS` in chunking.py, which decides whether to MERGE
a short section into its predecessor. This one decides whether a chunk is worth embedding at
all, and it applies to the body text *before* the title prefix is added — the prefix is
exactly the part that carries no per-chunk information.

The floor applies to any source, but never to a document's ONLY chunk. That distinction is
what makes it safe: a terse proposal is still a governance act with a vote attached, and the
eval set contains lookup questions against short ones — so if a document reduces to a single
chunk, that chunk *is* the document and is kept whatever its size. What gets dropped is a
contentless fragment inside a document that has substance elsewhere: a stray `Date: 2026-07-21`
/ `***` chunk in a long proposal, or a one-line "thanks!" reply in a busy thread.
"""

from __future__ import annotations

import re

# Titles Discourse ships with. Anchored where possible so a real proposal that happens to
# contain the word "welcome" is not swept up.
BOILERPLATE_TITLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^welcome to discourse\b", re.I),
    re.compile(r"^welcome to (the )?\w+ (forum|governance forum|community)!?$", re.I),
    re.compile(r"^about the .+ category$", re.I),
    re.compile(r"^(site feedback|feedback|meta|uncategorized)$", re.I),
    re.compile(r"^(readme|test topic|test post)$", re.I),
    re.compile(r"^introduce yourself\b", re.I),
    re.compile(r"^privacy policy$|^terms of service$|^faq$|^guidelines$", re.I),
)

# Below this many tokens of BODY text, a chunk is noise. Calibrated against the corpus: real
# governance content starts around 20 tokens, while the measured offenders sit at 4-8
# ("You need $ARB tokens", "As u wish of companies choice", "Mining,staking,delegate???").
MIN_BODY_TOKENS = 20


def is_boilerplate_topic(title: str | None) -> bool:
    """True for forum threads that are forum-software furniture rather than governance."""
    if not title:
        return False
    normalised = " ".join(title.split()).strip()
    return any(p.search(normalised) for p in BOILERPLATE_TITLE_PATTERNS)


def is_substantive_chunk(body_tokens: int, *, is_indivisible_document: bool = False) -> bool:
    """True when a chunk is worth embedding.

    `is_indivisible_document` exempts a chunk from the floor because dropping it would remove
    a whole governance act from the corpus. The caller decides, and the asymmetry is the
    point — it is NOT simply "this document has one chunk":

      * A PROPOSAL is one document with one vote attached. A terse proposal ("Enable the fee
        switch. Yes.") is still a governance act, the eval set contains lookup questions
        against short ones, and nothing else in the corpus stands in for it. Exempt.

      * A FORUM POST is one reply among many in a topic. A one-line post is not a governance
        act, and the topic remains fully retrievable through its substantive posts. Not
        exempt — measured, exempting them re-admitted "Thank you for the support!", "Stay
        Optimistic!" and "Great start. Excited to add value", and cost 0.05 of thematic recall.

    A chunk with NO body is never substantive, exemption or not. It carries only the title,
    which already prefixes every other chunk of its document, so it adds nothing and retrieves
    on title words alone. Worse, in silver such documents all share `content_hash = sha256("")`
    — 9 of them — so the UNIQUE constraint keeps exactly one and attributes it to an arbitrary
    document, producing a citation that names the wrong DAO.
    """
    if body_tokens <= 0:
        return False
    if is_indivisible_document:
        return True
    return body_tokens >= MIN_BODY_TOKENS
