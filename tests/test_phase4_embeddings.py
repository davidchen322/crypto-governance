"""Phase 4 acceptance: embedding batching and ordering.

No network, no docker, no cost. These cover the two ways a loader silently corrupts a
vector store: sending a batch the API rejects wholesale, and pairing returned vectors with
the wrong text.
"""

from __future__ import annotations

import pytest

from ai_agent.chains.embeddings import (
    MAX_BATCH_INPUTS,
    PendingChunk,
    batches,
    embed_batch,
)


def make_chunk(index: int, tokens: int = 100) -> PendingChunk:
    return PendingChunk(
        source="proposal",
        document_id=f"0x{index:04x}",
        protocol_name="aave",
        source_content_hash=f"hash{index}",
        chunk_type="proposal_body",
        chunk_index=index,
        heading=None,
        text=f"chunk text {index}",
        tokens=tokens,
        valid_from="2026-08-15 00:00:00",
        valid_to=None,
    )


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------


def test_every_chunk_appears_exactly_once():
    """The failure this guards: an off-by-one in the batch loop silently drops the last
    partial batch, so the tail of the corpus is never embedded and never reported missing."""
    chunks = [make_chunk(i) for i in range(500)]
    batched = [c for batch in batches(chunks) for c in batch]
    assert len(batched) == 500
    assert [c.chunk_index for c in batched] == list(range(500))


def test_batches_respect_the_input_count_limit():
    chunks = [make_chunk(i, tokens=1) for i in range(MAX_BATCH_INPUTS * 3)]
    assert all(len(b) <= MAX_BATCH_INPUTS for b in batches(chunks))


def test_batches_respect_the_token_limit():
    """Tokens bind before input count on long chunks. An oversized batch is rejected
    wholesale, losing the work of every input in it."""
    chunks = [make_chunk(i, tokens=8_000) for i in range(60)]
    for batch in batches(chunks):
        assert sum(c.tokens for c in batch) <= 100_000 or len(batch) == 1


def test_a_single_oversized_chunk_still_yields():
    """A chunk larger than the whole batch budget must not vanish or loop forever."""
    chunks = [make_chunk(0, tokens=200_000)]
    out = list(batches(chunks))
    assert len(out) == 1 and len(out[0]) == 1


def test_empty_input_yields_no_batches():
    assert list(batches([])) == []


# --------------------------------------------------------------------------
# Vector/text pairing
# --------------------------------------------------------------------------


class FakeHttp:
    """Returns embeddings deliberately out of order, which is permitted by the API."""

    def __init__(self, shuffle=True):
        self.shuffle = shuffle
        self.calls = []

    def request_json(self, method, url, **kwargs):
        texts = kwargs["json"]["input"]
        data = [{"index": i, "embedding": [float(i)] * 4} for i in range(len(texts))]
        if self.shuffle:
            data = list(reversed(data))
        self.calls.append(texts)
        return {"data": data, "usage": {"total_tokens": 42}}


def test_vectors_are_paired_by_index_not_arrival_order():
    """The API returns an `index` per embedding and does not promise list order. Trusting
    order silently attaches every vector to the wrong chunk — retrieval then returns
    confident nonsense with no error anywhere."""
    http = FakeHttp(shuffle=True)
    vectors, used = embed_batch(http, "sk-test", "m", 4, ["a", "b", "c"])
    assert vectors == [[0.0] * 4, [1.0] * 4, [2.0] * 4]
    assert used == 42


def test_dimensions_are_requested_explicitly():
    """text-embedding-3-large returns 3072 by default, which will not fit VECTOR(1536)
    and cannot be HNSW-indexed at all."""
    http = FakeHttp(shuffle=False)
    embed_batch(http, "sk-test", "text-embedding-3-large", 1536, ["a"])
    assert http.calls == [["a"]]


def test_mismatched_vector_count_raises_rather_than_truncating():
    """zip(strict=True) in insert() is what turns a short response into a loud failure
    instead of a partially-populated table."""

    class ShortHttp(FakeHttp):
        def request_json(self, method, url, **kwargs):
            return {"data": [{"index": 0, "embedding": [0.0]}], "usage": {}}

    vectors, _ = embed_batch(ShortHttp(), "sk-test", "m", 1, ["a", "b"])
    with pytest.raises(ValueError):
        list(zip([make_chunk(0), make_chunk(1)], vectors, strict=True))


# --------------------------------------------------------------------------
# Diversity and flatness — pure logic, no database, no API
# --------------------------------------------------------------------------


def make_result(source, doc_id, distance):
    from ai_agent.chains.retrieval import SearchResult

    return SearchResult(
        source=source,
        document_id=doc_id,
        protocol_name="aave",
        chunk_index=0,
        heading=None,
        text="t",
        distance=distance,
    )


def test_capping_per_document_replaces_duplicates_with_new_documents():
    """Measured before this existed: top-5 chunks averaged 2.0 distinct documents, so a
    question expecting four could not succeed at k=5 no matter how good the ranking."""
    from ai_agent.chains.retrieval import _cap_per_document

    ranked = [
        make_result("proposal", "A", 0.10),
        make_result("proposal", "A", 0.11),
        make_result("proposal", "A", 0.12),
        make_result("proposal", "B", 0.20),
        make_result("forum", "C", 0.30),
        make_result("proposal", "D", 0.40),
    ]
    kept = _cap_per_document(ranked, k=3, max_per_doc=1)
    assert [(r.source, r.document_id) for r in kept] == [
        ("proposal", "A"),
        ("proposal", "B"),
        ("forum", "C"),
    ]


def test_capping_preserves_rank_order():
    from ai_agent.chains.retrieval import _cap_per_document

    ranked = [make_result("proposal", chr(65 + i), 0.1 * i) for i in range(5)]
    kept = _cap_per_document(ranked, k=5, max_per_doc=1)
    assert [r.distance for r in kept] == sorted(r.distance for r in kept)


def test_same_document_id_in_different_sources_is_not_conflated():
    """A forum topic and a proposal can share a numeric id; only (source, id) is unique."""
    from ai_agent.chains.retrieval import _cap_per_document

    ranked = [make_result("proposal", "42", 0.1), make_result("forum", "42", 0.2)]
    assert len(_cap_per_document(ranked, k=5, max_per_doc=1)) == 2


def test_flat_distance_profile_reads_as_unanswerable():
    """An unanswerable question returns k uniformly-mediocre chunks. That flatness is the
    signal the absolute distance throws away."""
    from ai_agent.chains.retrieval import looks_unanswerable

    flat = [make_result("proposal", chr(65 + i), 0.44 + 0.001 * i) for i in range(10)]
    assert looks_unanswerable(flat, min_gap=0.025)


def test_a_clear_winner_reads_as_answerable():
    from ai_agent.chains.retrieval import looks_unanswerable

    peaked = [make_result("proposal", "A", 0.20)] + [
        make_result("proposal", chr(66 + i), 0.40 + 0.01 * i) for i in range(9)
    ]
    assert not looks_unanswerable(peaked, min_gap=0.025)


def test_gap_is_measured_over_a_fixed_window():
    """The bug this pins: the gap was fitted over 10 results but applied over the 20 that
    diversity over-fetches. A longer tail raises the mean and widens the gap for free, so
    two negatives the fit said would be blocked sailed through."""
    from ai_agent.chains.retrieval import GAP_WINDOW, looks_unanswerable

    flat10 = [make_result("proposal", chr(65 + i), 0.44 + 0.001 * i) for i in range(GAP_WINDOW)]
    # Same head, plus a long mediocre tail that would inflate an unwindowed mean.
    with_tail = flat10 + [make_result("proposal", f"T{i}", 0.60 + 0.01 * i) for i in range(20)]
    assert looks_unanswerable(flat10, min_gap=0.025)
    assert looks_unanswerable(with_tail, min_gap=0.025), (
        "verdict changed when a tail was appended — the window is not fixed"
    )


def test_too_few_results_is_not_treated_as_flat():
    from ai_agent.chains.retrieval import looks_unanswerable

    assert not looks_unanswerable([], min_gap=0.025)
    assert not looks_unanswerable([make_result("proposal", "A", 0.9)], min_gap=0.025)


# --------------------------------------------------------------------------
# The gap exemption — flatness alone cannot tell "nothing relevant" from
# "everything relevant"
# --------------------------------------------------------------------------


def test_a_densely_covered_question_is_not_treated_as_unanswerable():
    """The bug this pins. `cross-ens-next-era` scored recall 1.00 with a top-10 profile of
    0.345-0.369 — every hit strongly on topic — and the flatness test silenced it completely.
    In production that question retrieved perfectly and returned nothing.

    Flatness conflates two opposite situations: nothing is relevant, and everything is. Only
    absolute proximity distinguishes them."""
    from ai_agent.chains.retrieval import looks_unanswerable

    dense = [make_result("proposal", chr(65 + i), 0.345 + 0.0025 * i) for i in range(10)]
    gap = sum(r.distance for r in dense) / len(dense) - dense[0].distance
    assert gap < 0.025, "fixture must be flat, or it is not testing the exemption"
    assert not looks_unanswerable(dense, min_gap=0.025)


def test_a_flat_and_distant_profile_is_still_unanswerable():
    """The exemption must not swallow the signal it was carved out of. Across 24 eval questions
    and 17 adversarial probes the closest unanswerable question came was 0.4320, which is why
    the exemption sits at 0.42."""
    from ai_agent.chains.retrieval import looks_unanswerable

    flat = [make_result("proposal", chr(65 + i), 0.44 + 0.001 * i) for i in range(10)]
    assert looks_unanswerable(flat, min_gap=0.025)


def test_the_exemption_boundary_is_where_it_is_documented():
    from ai_agent.chains.retrieval import GAP_EXEMPT_DISTANCE, looks_unanswerable

    just_inside = [make_result("proposal", chr(65 + i), GAP_EXEMPT_DISTANCE) for i in range(10)]
    just_outside = [
        make_result("proposal", chr(65 + i), GAP_EXEMPT_DISTANCE + 0.001) for i in range(10)
    ]
    assert not looks_unanswerable(just_inside, min_gap=0.025)
    assert looks_unanswerable(just_outside, min_gap=0.025)
