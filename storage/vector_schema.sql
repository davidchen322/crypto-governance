-- Semantic store. Applied automatically by the postgres init hook.
--
-- Two columns the source blueprint omitted carry most of the weight here:
--   embedding_model      lets two models' vectors coexist, so a model swap can be
--                        A/B tested instead of being a truncate-and-pray migration
--   source_content_hash  joins a chunk back to the exact document version it came from
--
-- DIMENSION CEILING — read before changing embedding models.
--
-- The `vector` type stores up to 16000 dimensions but HNSW and IVFFlat both refuse to
-- index more than 2000:
--
--     ERROR: column cannot have more than 2000 dimensions for hnsw index
--
-- So VECTOR(3072) is not "a bigger column" — it is an unindexable one, and every
-- similarity query degrades to a sequential scan over the whole corpus. Verified against
-- pgvector 0.8.6.
--
-- Escape hatch if a 3072-dimension model ever wins on the Phase 4 eval set: switch to
-- `halfvec` (float16), whose index ceiling is 4000.
--
--     ALTER TABLE document_embeddings ADD COLUMN embedding_3072 halfvec(3072);
--     CREATE INDEX ON document_embeddings USING hnsw (embedding_3072 halfvec_cosine_ops);
--
-- halfvec(3072) occupies exactly the same bytes as vector(1536) — 3072 x 2 == 1536 x 4 —
-- so that migration costs no additional storage. Measured at 20k rows: 159 MB heap+TOAST
-- and an 81 MB index, against 159 MB / 91 MB for vector(1536).
--
-- To keep float32 precision and still index, index a cast expression instead, and repeat
-- the cast in every query or the index will not be used:
--
--     CREATE INDEX ON document_embeddings
--         USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);
--
-- Usually unnecessary: text-embedding-3 models are Matryoshka-trained, so requesting
-- `dimensions=1536` from the larger model beats the smaller model at the same width and
-- keeps everything below the ceiling. Prefer that over widening the column.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS document_embeddings (
    chunk_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- `source` + `document_id` together identify the document a chunk came from, and map
    -- directly onto the eval set's label shape ({source: proposal, id: ...} /
    -- {source: forum, topic_id: ...}). A single proposal_id column could not name a forum
    -- post without overloading its meaning.
    source               VARCHAR(20)  NOT NULL,   -- 'proposal' | 'forum'
    document_id          VARCHAR(255) NOT NULL,   -- proposal_id, or topic_id for forum posts
    protocol_name        VARCHAR(100) NOT NULL,

    source_content_hash  VARCHAR(64)  NOT NULL,   -- silver's text identity; drives re-embedding
    chunk_type           VARCHAR(50)  NOT NULL,   -- proposal_body | forum_post | contract_source
    chunk_index          INT          NOT NULL,
    heading              VARCHAR(255),            -- section the chunk came from, for citations
    text_chunk           TEXT         NOT NULL,
    token_count          INT,

    embedding_model      VARCHAR(100) NOT NULL,
    -- How the text was chunked. Part of the identity because the loader's skip check is
    -- keyed on the SOURCE hash, which does not move when chunking logic changes — the same
    -- trap that let a stale Phase 3 derivation survive a "successful" rebuild. Two schemes
    -- coexist so a change can be A/B tested and reverted without re-embedding.
    chunk_scheme         VARCHAR(20)  NOT NULL DEFAULT 'v1',
    embedding            VECTOR(1536) NOT NULL,

    -- Carried from silver so retrieval can be asked historical questions without
    -- returning today's text and citing it as March's.
    valid_from           TIMESTAMPTZ  NOT NULL,
    valid_to             TIMESTAMPTZ,

    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Makes the loader idempotent: a re-run resolves to rows that already exist, so
    -- unchanged text is never paid for twice.
    CONSTRAINT document_embeddings_chunk_unique
        UNIQUE (source_content_hash, chunk_index, embedding_model, chunk_scheme)
);

-- Vectors from different models share no coordinate space, so every similarity query
-- must filter on embedding_model. This index supports that filter.
CREATE INDEX IF NOT EXISTS document_embeddings_lookup_idx
    ON document_embeddings (protocol_name, source, document_id, embedding_model, chunk_scheme);

CREATE INDEX IF NOT EXISTS document_embeddings_hnsw_idx
    ON document_embeddings USING hnsw (embedding vector_cosine_ops);
