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
    proposal_id          VARCHAR(255) NOT NULL,
    protocol_name        VARCHAR(100) NOT NULL,
    source_content_hash  VARCHAR(64)  NOT NULL,
    chunk_type           VARCHAR(50)  NOT NULL,
    chunk_index          INT          NOT NULL,
    text_chunk           TEXT         NOT NULL,
    embedding_model      VARCHAR(100) NOT NULL,
    embedding            VECTOR(1536) NOT NULL,
    valid_from           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    valid_to             TIMESTAMPTZ,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT document_embeddings_chunk_unique
        UNIQUE (source_content_hash, chunk_index, embedding_model)
);

-- Vectors from different models share no coordinate space, so every similarity query
-- must filter on embedding_model. This index supports that filter.
CREATE INDEX IF NOT EXISTS document_embeddings_lookup_idx
    ON document_embeddings (protocol_name, proposal_id, embedding_model);

CREATE INDEX IF NOT EXISTS document_embeddings_hnsw_idx
    ON document_embeddings USING hnsw (embedding vector_cosine_ops);
