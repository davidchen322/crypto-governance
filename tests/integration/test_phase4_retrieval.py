"""Phase 4 acceptance: the vector store and retrieval.

Split by cost. Everything up to `test_search_*` runs against Postgres alone with
hand-inserted vectors — no API calls, so it runs in CI and on a machine with no credential.
The `live` tests embed a real query and cost fractions of a cent.

The invariants here are the ones that fail silently: a query embedded with a different
model than the corpus returns confident nonsense, and a missing `embedding_model` filter
compares coordinates from unrelated spaces.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from ai_agent.chains.retrieval import search
from config.settings import Settings

pytestmark = pytest.mark.integration

MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
DIMS = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))


@pytest.fixture
def conn():
    with psycopg.connect(Settings.from_env().postgres_dsn, autocommit=True) as c:
        yield c


# --------------------------------------------------------------------------
# Schema and corpus state — no API calls
# --------------------------------------------------------------------------


def test_embedding_column_matches_the_configured_dimension(conn):
    """A mismatch here is not a runtime error — inserts fail one at a time, so a loader
    can appear to run and write nothing."""
    dim = conn.execute(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = 'document_embeddings'::regclass AND attname = 'embedding'"
    ).fetchone()[0]
    assert dim == DIMS


def test_corpus_is_embedded(conn):
    count = conn.execute("SELECT count(*) FROM document_embeddings").fetchone()[0]
    assert count > 1000, f"only {count} chunks embedded — run `make embed`"


def test_every_chunk_has_a_resolvable_document_identity(conn):
    """`source` + `document_id` is what the eval set joins on. A null or empty either side
    makes a chunk unscoreable and, later, uncitable."""
    bad = conn.execute(
        "SELECT count(*) FROM document_embeddings "
        "WHERE document_id IS NULL OR document_id = '' "
        "   OR source NOT IN ('proposal', 'forum')"
    ).fetchone()[0]
    assert bad == 0


def test_no_duplicate_chunks_for_one_model(conn):
    """The UNIQUE constraint is what makes the loader idempotent; this proves it holds."""
    dupes = conn.execute(
        "SELECT count(*) FROM (SELECT source_content_hash, chunk_index, embedding_model "
        "FROM document_embeddings GROUP BY 1,2,3 HAVING count(*) > 1) x"
    ).fetchone()[0]
    assert dupes == 0


def test_stored_vectors_are_unit_normalised(conn):
    """Cosine distance assumes it. A batch inserted un-normalised would rank plausibly but
    wrongly, with nothing in the logs to suggest a problem."""
    worst = conn.execute(
        # `<#>` is NEGATIVE inner product, so v <#> v == -(v . v) and the L2 norm is
        # sqrt(-(v <#> v)). Writing it as `v <#> v * -1` is a type error, not a subtle bug.
        "SELECT max(abs(1 - sqrt(-(embedding <#> embedding)))) FROM document_embeddings"
    ).fetchone()[0]
    assert worst is None or worst < 0.01, f"worst L2 norm deviation {worst}"


def test_hnsw_index_exists_and_is_used(conn):
    """Without the index every query is a sequential scan — correct, and unusably slow
    once the corpus grows."""
    plan = conn.execute(
        "EXPLAIN SELECT chunk_id FROM document_embeddings "
        "ORDER BY embedding <=> (SELECT embedding FROM document_embeddings LIMIT 1) LIMIT 5"
    ).fetchall()
    text = " ".join(r[0] for r in plan)
    assert "document_embeddings_hnsw_idx" in text, f"HNSW index not used:\n{text}"


def test_validity_windows_came_through_from_silver(conn):
    """Point-in-time retrieval depends on these; all-null means the column was dropped
    somewhere in the loader and `as_of` would silently return everything."""
    nulls = conn.execute(
        "SELECT count(*) FROM document_embeddings WHERE valid_from IS NULL"
    ).fetchone()[0]
    assert nulls == 0


# --------------------------------------------------------------------------
# Retrieval — these embed a query, so they cost a fraction of a cent
# --------------------------------------------------------------------------


@pytest.mark.live
def test_search_returns_ranked_results():
    results = search("Chainlink oracle deprecation for long-tail assets", k=5, max_distance=None)
    assert results
    distances = [r.distance for r in results]
    assert distances == sorted(distances), "results are not ordered by distance"


@pytest.mark.live
def test_search_finds_the_document_a_question_is_about():
    """The end-to-end smoke test: a question written against a known proposal retrieves it."""
    results = search(
        "Which Aave proposal deprecates Chainlink price feeds?", k=5, max_distance=None
    )
    ids = {r.document_id for r in results}
    assert "0xaa683250ff2e2835b9ac945d6219a35cdca1d4134f49e3bce6763ca8d8944c08" in ids


@pytest.mark.live
def test_threshold_rejects_distant_matches():
    """Vector search always returns k results. Without a cutoff there is no way to say
    'nothing here is close enough', which is what the negative questions measure."""
    loose = search("What is the current price of UNI?", k=5, max_distance=None)
    tight = search("What is the current price of UNI?", k=5, max_distance=0.30)
    assert loose, "expected the unfiltered search to return something"
    assert len(tight) < len(loose)


@pytest.mark.live
def test_protocol_filter_narrows_results():
    results = search("treasury management", k=10, max_distance=None, protocol="arbitrum")
    assert results
    assert {r.protocol_name for r in results} == {"arbitrum"}


@pytest.mark.live
def test_source_filter_selects_forum_or_proposal():
    forum = search("delegate discussion", k=5, max_distance=None, source="forum")
    assert forum and {r.source for r in forum} == {"forum"}


@pytest.mark.live
def test_point_in_time_search_excludes_unobserved_rows():
    """`as_of` before anything was harvested must return nothing — otherwise a historical
    question silently retrieves today's text."""
    from datetime import UTC, datetime

    past = datetime(2020, 1, 1, tzinfo=UTC)
    assert search("governance", k=5, max_distance=None, as_of=past) == []
