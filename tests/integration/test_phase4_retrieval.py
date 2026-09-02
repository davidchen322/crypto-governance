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

from ai_agent.chains.chunking import CHUNK_SCHEME
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
    """The bound here is deliberately tied to the committed CI fixture (see
    tests/fixtures/README.md), not to a full production-scale corpus — a developer's own
    local backfill will clear it by a wide margin; the fixture (~1,070 rows across two
    chunk schemes) clears it comfortably too."""
    count = conn.execute("SELECT count(*) FROM document_embeddings").fetchone()[0]
    assert count > 400, f"only {count} chunks embedded — run `make embed`"


def test_every_chunk_has_a_resolvable_document_identity(conn):
    """`source` + `document_id` is what the eval set joins on. A null or empty either side
    makes a chunk unscoreable and, later, uncitable."""
    bad = conn.execute(
        "SELECT count(*) FROM document_embeddings "
        "WHERE document_id IS NULL OR document_id = '' "
        "   OR source NOT IN ('proposal', 'forum')"
    ).fetchone()[0]
    assert bad == 0


def test_no_duplicate_chunks_for_one_model_and_scheme(conn):
    """The UNIQUE constraint is what makes the loader idempotent; this proves it holds.

    `chunk_scheme` belongs in the grouping for the same reason it belongs in the constraint:
    the same source text chunked two ways is two legitimate rows, not a duplicate. Omitting
    it here reported all 1,655 v1 rows as duplicates the moment v2 landed."""
    dupes = conn.execute(
        "SELECT count(*) FROM (SELECT source_content_hash, chunk_index, embedding_model, "
        "chunk_scheme FROM document_embeddings GROUP BY 1,2,3,4 HAVING count(*) > 1) x"
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


def test_hnsw_index_can_serve_the_ordering(conn):
    """Without a usable index every query is a sequential scan — correct, and unusably slow
    once the corpus grows.

    Asserted with `enable_seqscan = off` rather than against the planner's free choice. At
    corpus scale the planner rightly prefers a seq scan: HNSW costs ~1,888 to start against
    ~594 to sort 3,310 rows outright. The unforced version passed at 1,655 rows and failed
    at 3,310 without anything being wrong — it was measuring table size, not the index.

    The teeth are in the *shape* of the forced plan: a wrong operator class (`vector_l2_ops`
    under a `<=>` query) leaves the index unusable for the ordering, and the plan falls back
    to a Sort node. That is the failure worth catching, and this still catches it."""
    conn.execute("SET enable_seqscan = off")
    plan = conn.execute(
        "EXPLAIN SELECT chunk_id FROM document_embeddings "
        "ORDER BY embedding <=> (SELECT embedding FROM document_embeddings LIMIT 1) LIMIT 5"
    ).fetchall()
    conn.execute("SET enable_seqscan = on")
    text = " ".join(r[0] for r in plan)
    assert "document_embeddings_hnsw_idx" in text, f"HNSW index not usable:\n{text}"
    assert "Sort Key: ((document_embeddings.embedding <=>" not in text, (
        f"index present but the ordering fell back to a sort:\n{text}"
    )


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
    'nothing here is close enough', which is what the negative questions measure.

    Both cutoffs are disabled on the loose call, not just `max_distance`. Leaving `min_gap`
    at its default meant the control arm was also filtered, and the test passed only while
    this question's distance profile happened not to look flat — under chunk scheme v2 it
    does look flat (correctly: it is a negative), and the control returned nothing."""
    loose = search("What is the current price of UNI?", k=5, max_distance=None, min_gap=None)
    tight = search("What is the current price of UNI?", k=5, max_distance=0.30, min_gap=None)
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


# --------------------------------------------------------------------------
# Chunk scheme — the A/B mechanism
# --------------------------------------------------------------------------


def test_both_chunk_schemes_are_stored(conn):
    """Keeping v1 alongside v2 makes reverting a one-constant change rather than a
    re-embed. The vectors are already paid for; discarding them buys nothing.

    The per-scheme bound (like `test_corpus_is_embedded`'s) is sized to the committed CI
    fixture, which was deliberately embedded under both schemes specifically to prove this
    coexistence — see tests/fixtures/README.md."""
    schemes = dict(
        conn.execute("SELECT chunk_scheme, count(*) FROM document_embeddings GROUP BY 1").fetchall()
    )
    assert len(schemes) >= 2, f"expected v1 and v2 to coexist, found {schemes}"
    assert all(n > 150 for n in schemes.values())


def test_uniqueness_includes_the_chunk_scheme(conn):
    """Without the scheme in the key, changing chunking logic leaves the loader's skip
    check unmoved — it reports 'already embedded' and silently serves stale vectors. The
    same trap that let a stale Phase 3 derivation survive a successful-looking rebuild."""
    cols = conn.execute("""
        SELECT a.attname FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
        WHERE c.conname = 'document_embeddings_chunk_unique'
    """).fetchall()
    assert "chunk_scheme" in {r[0] for r in cols}


def test_v2_chunks_carry_the_protocol_label(conn):
    """The whole point of v2: `protocol_name` is a column, and a column is invisible to a
    vector. The label has to be in the text."""
    bad = conn.execute("""
        SELECT count(*) FROM document_embeddings
        WHERE chunk_scheme = 'v2'
          AND text_chunk NOT LIKE 'Aave —%' AND text_chunk NOT LIKE 'Uniswap —%'
          AND text_chunk NOT LIKE 'Arbitrum —%' AND text_chunk NOT LIKE 'Optimism —%'
          AND text_chunk NOT LIKE 'ENS —%'
    """).fetchone()[0]
    assert bad == 0, f"{bad} v2 chunks have no protocol label"


@pytest.mark.live
def test_search_returns_only_the_active_scheme():
    """Mixing schemes in one similarity search is the same error as mixing models —
    different text produces different vectors, and ranking across both is meaningless."""
    from ai_agent.chains.chunking import CHUNK_SCHEME

    results = search("oracle deprecation", k=5, max_distance=None, min_gap=None)
    assert results
    prefixes = {"v2": ("Aave —", "Uniswap —", "Arbitrum —", "Optimism —", "ENS —")}
    if CHUNK_SCHEME == "v2":
        assert all(r.text.startswith(prefixes["v2"]) for r in results)


# --------------------------------------------------------------------------
# Citation metadata — carried from silver, never used for ranking
# --------------------------------------------------------------------------


def test_every_chunk_has_a_title_and_a_governance_date(conn):
    """Without these a result can only be shown as a 66-character hash."""
    missing = conn.execute(
        "SELECT count(*) FROM document_embeddings WHERE title IS NULL OR document_date IS NULL"
    ).fetchone()[0]
    assert missing == 0


def test_document_date_is_not_the_harvest_date(conn):
    """The trap: `valid_from` is when the pipeline observed a row. This corpus was harvested
    in one pass, so every valid_from lands on the same day. A `document_date` that mirrored
    it would render the harvest date on every result and look perfectly plausible.

    The governance dates must span years, because the proposals do."""
    span = conn.execute(
        "SELECT count(DISTINCT document_date::date), count(DISTINCT valid_from::date) "
        "FROM document_embeddings"
    ).fetchone()
    assert span[1] <= 2, "valid_from should be ~one harvest day; the premise has changed"
    assert span[0] > 50, f"only {span[0]} distinct governance dates — looks like valid_from"


def test_document_dates_are_not_in_the_future(conn):
    """A unix-seconds field decoded as milliseconds lands in the year 57000 and nothing
    upstream complains."""
    bad = conn.execute(
        "SELECT count(*) FROM document_embeddings WHERE document_date > now() + interval '1 day'"
    ).fetchone()[0]
    assert bad == 0


def test_titles_match_the_prefix_embedded_in_v2_chunk_text(conn):
    """The stored title and the title inside the embedded text come from the same silver
    column. If they drift, citations name one document and the evidence quotes another."""
    mismatched = conn.execute("""
        SELECT count(*) FROM document_embeddings
        WHERE chunk_scheme = 'v2' AND title IS NOT NULL AND length(title) > 12
          AND position(substring(title from 1 for 12) in text_chunk) = 0
    """).fetchone()[0]
    assert mismatched == 0


# --------------------------------------------------------------------------
# Curation — the corpus must stay clean, not just be cleaned once
# --------------------------------------------------------------------------


def test_no_platform_boilerplate_survives_in_the_corpus(conn):
    """Discourse's default welcome topic supplied the top hit for 'how many cats climbed up my
    tree?' and caused a market-cap question to pass both cutoffs on a 14-token reply."""
    from config.corpus_filters import is_boilerplate_topic

    titles = conn.execute(
        "SELECT DISTINCT title FROM document_embeddings WHERE source='forum' AND title IS NOT NULL"
    ).fetchall()
    offenders = [t for (t,) in titles if is_boilerplate_topic(t)]
    assert offenders == [], f"boilerplate still embedded: {offenders}"


def test_no_contentless_chunks_survive_in_the_corpus(conn):
    """A chunk carrying a handful of words is mostly protocol-name under v2, so it sits near
    any query naming that protocol regardless of subject."""
    worst = conn.execute(
        "SELECT min(token_count) FROM document_embeddings WHERE chunk_scheme = %s",
        (CHUNK_SCHEME,),
    ).fetchone()[0]
    assert worst >= 20, f"shortest chunk is {worst} tokens"


def test_the_degenerate_empty_content_hash_is_absent(conn):
    """sha256('') — every empty-bodied post shares it, so the UNIQUE constraint keeps one row
    and attributes it to an arbitrary document. Nine silver posts qualify.

    Scoped to the ACTIVE scheme. A retired scheme is pruned for boilerplate only, because that
    rule depends on the thread title alone; the token floor cannot be applied to it, since
    today's chunker cannot reproduce a retired chunker's boundaries and guessing at them would
    delete real rows. Retired vectors exist so a chunking change can be reverted without
    paying to re-embed, and only the active scheme is ever searched."""
    n = conn.execute(
        "SELECT count(*) FROM document_embeddings "
        "WHERE source_content_hash = %s AND chunk_scheme = %s",
        ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", CHUNK_SCHEME),
    ).fetchone()[0]
    assert n == 0


def test_the_corpus_matches_what_the_loader_would_produce(conn):
    """Drift check. If these disagree, the next `make embed` silently changes retrieval results
    and every recorded number becomes unattributable."""
    from ai_agent.chains.embeddings import load_silver_chunks

    produced = {
        (c.source, c.document_id, c.source_content_hash, c.chunk_index)
        for c in load_silver_chunks()
    }
    stored = {
        tuple(r)
        for r in conn.execute(
            "SELECT source, document_id, source_content_hash, chunk_index "
            "FROM document_embeddings WHERE chunk_scheme = %s",
            (CHUNK_SCHEME,),
        ).fetchall()
    }
    assert not (stored - produced), (
        f"{len(stored - produced)} stored chunk(s) the loader no longer produces"
    )
