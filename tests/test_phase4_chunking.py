"""Phase 4 acceptance: chunking.

Pure functions, no network, no docker. Chunking is where retrieval quality is silently
won or lost — a chunk that splits a specification table in half, or one that is mostly
heading, degrades every downstream number without ever raising an error.
"""

from __future__ import annotations

from ai_agent.chains.chunking import (
    Chunk,
    chunk_document,
    chunk_forum_post,
    chunk_proposal,
    count_tokens,
)

# Section lengths are modelled on the real corpus, where a proposal section runs 100-400
# tokens. An earlier version of this fixture used one-line sections, which tripped the
# runt-merge and made the test look like a chunking bug rather than an unrepresentative
# fixture.
PROPOSAL = """## Summary
LlamaRisk proposes deprecating Chainlink price feeds for a set of long-tail assets
deployed across Aave V2 or V3 reserves. These are feeds that Chainlink itself has
classified as high or very high operational risk, as the underlying assets have lost
meaningful adoption and liquidity, leaving too little trading activity to price them
reliably across the venues the aggregator samples.

## Motivation
Continuing to rely on a price feed whose underlying market has thinned out exposes the
protocol to manipulation: a small amount of capital can move a shallow market far enough
to trigger liquidations or permit borrowing against an inflated valuation. Deprecating the
feed removes that surface before it is exploited rather than after.

## Specification
The set spans 10 deployments, part of which are already deprecated, with aggregate
affected supply of $6.76M and aggregate debt of $4.29M. Each reserve is frozen, its LTV
set to zero, and its oracle replaced with a fixed-price adapter so existing positions can
still be liquidated in an orderly fashion rather than becoming unwindable.
"""


def test_empty_input_produces_no_chunks():
    assert chunk_document("") == []
    assert chunk_document("   \n  ") == []
    assert chunk_proposal(None, None) == []


def test_short_document_is_one_chunk():
    chunks = chunk_document("A single short paragraph about fee parameters.")
    assert len(chunks) == 1
    assert chunks[0].index == 0


def test_headings_become_separate_chunks():
    chunks = chunk_document(PROPOSAL)
    headings = [c.heading for c in chunks]
    assert "Summary" in headings
    assert "Motivation" in headings
    assert "Specification" in headings


def test_each_chunk_carries_its_heading_in_the_text():
    """A window from deep inside a section is context-free without this — the embedding
    cannot see the row's other columns."""
    for chunk in chunk_document(PROPOSAL):
        if chunk.heading:
            assert chunk.text.startswith(chunk.heading)


def test_indexes_are_contiguous_from_zero():
    chunks = chunk_document(PROPOSAL)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_long_section_is_windowed():
    long_body = "## Specification\n" + ("The parameter is set to a new value. " * 400)
    chunks = chunk_document(long_body, target_tokens=200, overlap_tokens=40)
    assert len(chunks) > 1
    assert all(c.tokens <= 260 for c in chunks), [c.tokens for c in chunks]


def test_windows_overlap_so_a_straddling_sentence_survives():
    """Without overlap, a sentence crossing a boundary is retrievable from neither side."""
    body = " ".join(f"Sentence number {i} about governance parameters." for i in range(300))
    chunks = chunk_document(body, target_tokens=200, overlap_tokens=60)
    assert len(chunks) > 1
    first_tail = chunks[0].text.split()[-15:]
    assert any(word in chunks[1].text for word in first_tail), (
        "consecutive chunks share no text — overlap is not being applied"
    )


def test_runt_sections_are_merged():
    """A chunk that is only a heading embeds to a vector matching almost anything."""
    text = "## A\n\n## B\n\n## C\n\nThe only real content lives here and is reasonably long."
    chunks = chunk_document(text)
    assert all(c.tokens >= 10 for c in chunks), [(c.heading, c.tokens) for c in chunks]


def test_proposal_title_is_prepended_to_every_chunk():
    """Findability: a chunk about fee parameters should say which proposal it belongs to."""
    title = "[Temp Check] Activate v4 Protocol Fees"
    chunks = chunk_proposal(title, PROPOSAL)
    assert chunks
    assert all(c.text.startswith(title) for c in chunks)


def test_proposal_with_title_but_no_body_still_chunks():
    chunks = chunk_proposal("Just a title", "")
    assert len(chunks) == 1
    assert "Just a title" in chunks[0].text


def test_forum_post_carries_thread_title():
    chunks = chunk_forum_post(
        "Lombard LBTC backing strategy change", "I have seen very little written about this."
    )
    assert chunks
    assert all("Lombard LBTC" in c.text for c in chunks)


def test_token_counts_are_real_not_estimated():
    """chars/4 drifts badly on addresses and code; the loader's cost and the model's hard
    limit are both denominated in real tokens."""
    text = "0xaa683250ff2e2835b9ac945d6219a35cdca1d4134f49e3bce6763ca8d8944c08"
    assert count_tokens(text) != len(text) // 4


def test_chunking_is_deterministic():
    """Re-chunking unchanged text must produce identical chunks, or content_hash stops
    predicting whether a re-embed is needed."""
    assert chunk_proposal("T", PROPOSAL) == chunk_proposal("T", PROPOSAL)


def test_chunk_is_hashable_and_comparable():
    a = Chunk(text="x", index=0, tokens=1, heading=None)
    b = Chunk(text="x", index=0, tokens=1, heading=None)
    assert a == b
    assert len({a, b}) == 1


def test_single_line_document_starting_with_hash_is_not_swallowed():
    """A heading consuming the whole document yields one section with an empty body, which
    the section loop drops — silently discarding the document. A real 2,123-character
    Arbitrum milestone report was lost this way."""
    text = "# Stylus Toolkit Final Report Grant Recipient: someone. " + ("Body text here. " * 30)
    chunks = chunk_document(text)
    assert chunks, "document starting with '#' on one line produced no chunks"
    assert "Stylus Toolkit" in chunks[0].text
    assert sum(c.tokens for c in chunks) > 50


def test_heading_followed_by_content_still_splits_normally():
    """The guard above must not disable heading splitting when there IS a body."""
    text = "# Only Heading\n\n" + ("Real content follows and is long enough to matter. " * 20)
    chunks = chunk_document(text)
    assert chunks[0].heading == "Only Heading"
