-- Semantic store. Applied automatically by the postgres init hook.
--
-- Two columns the source blueprint omitted carry most of the weight here:
--   embedding_model      lets two models' vectors coexist, so a model swap can be
--                        A/B tested instead of being a truncate-and-pray migration
--   source_content_hash  joins a chunk back to the exact document version it came from
--
-- Note: vector dimension is fixed per column. VECTOR(1536) fits text-embedding-3-small;
-- a 3072-dimension model needs a separate column or table, not just new rows.

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
