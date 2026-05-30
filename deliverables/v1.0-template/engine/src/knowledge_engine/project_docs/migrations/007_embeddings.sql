-- Migration 007 — Optional embedding metadata. Vectors are stored as packed
-- float blobs (4 bytes/float), mirroring the base engine's embeddings module.
-- This table is always created but only populated when embeddings are enabled.

CREATE TABLE IF NOT EXISTS doc_embeddings (
    record_id  TEXT NOT NULL REFERENCES project_docs(record_id) ON DELETE CASCADE,
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (record_id, provider, model)
);
