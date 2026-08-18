"""Phase 4 acceptance: corpus curation.

No database, no API. These pin the two rules that keep low-content text out of the semantic
corpus, and — just as importantly — pin what those rules must NOT remove. A curation filter
that is slightly too aggressive deletes governance history and reports success.
"""

from __future__ import annotations

import pytest

from ai_agent.chains.chunking import chunk_forum_post, chunk_proposal, count_tokens
from config.corpus_filters import (
    MIN_BODY_TOKENS,
    is_boilerplate_topic,
    is_substantive_chunk,
)

# --------------------------------------------------------------------------
# Rule 1 — platform boilerplate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Welcome to Discourse",
        "welcome to discourse",
        "  Welcome to Discourse  ",
        "Welcome to Discourse!",
        "About the Governance category",
        "About the Proposals category",
        "Site Feedback",
        "Uncategorized",
        "Introduce yourself",
        "Introduce Yourself!",
        "FAQ",
        "Guidelines",
    ],
)
def test_platform_furniture_is_recognised(title):
    """Measured motivation: Arbitrum's copy of Discourse's default welcome topic supplied the
    top hit for 'how many cats climbed up my tree?' and caused a market-cap question to pass
    both relevance cutoffs with a 14-token reply."""
    assert is_boilerplate_topic(title)


@pytest.mark.parametrize(
    "title",
    [
        "[ARFC] Oracle Deprecation for Long-tail Assets Across Aave V2 and V3",
        "[Temp Check] - Four for V4",
        "Welcoming new delegates to the Arbitrum ecosystem",
        "A warm welcome to our newly elected council members",
        "About the treasury diversification proposal",
        "Feedback on the proposed fee switch",
        "Test deployment of Aave V4 on Arc",
        "Guidelines for delegate compensation",
        "[Constitutional] AIP: ArbOS 60 Elara",
    ],
)
def test_real_governance_threads_are_not_swept_up(title):
    """The expensive failure mode. 'About the treasury diversification proposal' is a real
    thread and must survive a pattern aimed at 'About the ... category'; 'Guidelines for
    delegate compensation' must survive one aimed at a bare 'Guidelines'."""
    assert not is_boilerplate_topic(title)


def test_missing_title_is_not_boilerplate():
    """A null title is unknown, not furniture — dropping it would silently lose real posts."""
    assert not is_boilerplate_topic(None)
    assert not is_boilerplate_topic("")


# --------------------------------------------------------------------------
# Rule 2 — chunks with negligible content
# --------------------------------------------------------------------------


def test_the_measured_offenders_are_rejected():
    """The exact strings that polluted retrieval, by their real body-token counts."""
    for text in (
        "You need $ARB tokens",
        "As u wish of companies choice",
        "Mining,staking,delegate???",
    ):
        assert not is_substantive_chunk(count_tokens(text)), text


def test_substantial_text_is_kept():
    body = (
        "The reserves in scope share a common deprecation reason: each underlying asset has "
        "either lost meaningful adoption or held its peg only intermittently."
    )
    assert is_substantive_chunk(count_tokens(body))


def test_the_floor_is_applied_at_the_boundary():
    assert not is_substantive_chunk(MIN_BODY_TOKENS - 1)
    assert is_substantive_chunk(MIN_BODY_TOKENS)


# --------------------------------------------------------------------------
# body_tokens — the quantity the rule is applied to
# --------------------------------------------------------------------------


def test_body_tokens_excludes_the_protocol_and_title_prefix():
    """This is the whole reason `body_tokens` exists. Judging a chunk by `tokens` would let a
    long title carry a contentless reply over the floor — and under v2 that prefix is exactly
    what makes a runt chunk match anything mentioning its protocol."""
    body = "You need $ARB tokens"
    chunks = chunk_forum_post("Welcome to Discourse", body, protocol="Arbitrum")
    assert len(chunks) == 1
    c = chunks[0]
    assert c.body_tokens == count_tokens(body)
    assert c.tokens > c.body_tokens, "prefix should inflate `tokens` but not `body_tokens`"
    assert not is_substantive_chunk(c.body_tokens)
    # The inflation is large relative to the content — the failure mode, quantified.
    assert c.tokens >= 2 * c.body_tokens


def test_body_tokens_survives_the_whole_chunking_path():
    """A long, sectioned document must still report per-chunk body sizes, not the prefixed
    totals, or the floor would silently never fire on real content."""
    body = "## Summary\n\n" + ("The council reviewed the proposal in detail. " * 60)
    chunks = chunk_proposal("A Very Long Proposal Title Indeed", body, protocol="Aave")
    assert len(chunks) > 1
    for c in chunks:
        assert c.body_tokens > 0
        assert c.body_tokens <= c.tokens


def test_a_short_proposal_is_exempt_because_nothing_stands_in_for_it():
    """A terse proposal is still a governance act with a vote attached, and the eval set
    contains lookup questions against short ones. Dropping it removes the act entirely."""
    chunks = chunk_proposal("Enable the fee switch", "Yes.", protocol="Uniswap")
    assert len(chunks) == 1
    assert chunks[0].body_tokens < MIN_BODY_TOKENS
    assert is_substantive_chunk(chunks[0].body_tokens, is_indivisible_document=True)


def test_a_short_forum_reply_is_not_exempt():
    """The asymmetry. A one-line reply is not a governance act and its topic stays retrievable
    through the posts that carry substance. Measured: granting forum posts the same exemption
    re-admitted 'Thank you for the support!' and 'Stay Optimistic!' and cost 0.05 of thematic
    recall (0.75 -> 0.70)."""
    chunks = chunk_forum_post("PGov - Delegate Communication Thread", "Thank you for the support!")
    assert len(chunks) == 1
    assert not is_substantive_chunk(chunks[0].body_tokens)


def test_an_empty_body_is_never_substantive_even_when_exempt():
    """The exemption must not rescue a title-only chunk. In silver, 9 forum posts have an empty
    body and therefore all share content_hash = sha256(''), so the UNIQUE constraint keeps
    exactly one and attributes it to an arbitrary document — a citation naming the wrong DAO."""
    chunks = chunk_forum_post("Governance Process", "", protocol="ENS")
    assert len(chunks) == 1
    assert chunks[0].body_tokens == 0, "a title-only chunk must report zero body tokens"
    assert not is_substantive_chunk(0, is_indivisible_document=True)


def test_the_empty_hash_collision_is_real():
    """Pins the mechanism rather than trusting the story: every empty body hashes alike."""
    import hashlib

    assert (
        hashlib.sha256(b"").hexdigest()
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
