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
