-- knowledge.db schema version 2: evidence store (V2 P3/P4, ADR 0012).
-- Consumers: knowledge_engine.knowledge.evidence.*.
-- Identities are derived strings (revision_id, evidence_id; ADR 0007), never row
-- ids. The INTEGER keys below (seq) order rows internally and are never exposed
-- as an identity. Never edit this file once shipped; add 003_*.sql instead.

-- Rights, retention and bibliographic metadata of a record; the current head
-- revision; and the deletion marker (a tombstoned record keeps its identity).
ALTER TABLE records ADD COLUMN retention TEXT NOT NULL DEFAULT 'retain'
    CHECK (retention IN ('retain', 'digest_only', 'tombstone'));
ALTER TABLE records ADD COLUMN license TEXT;
ALTER TABLE records ADD COLUMN rights_notes TEXT;
ALTER TABLE records ADD COLUMN bibliographic_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE records ADD COLUMN current_revision_id TEXT;
ALTER TABLE records ADD COLUMN deleted_at TEXT;

-- Former logical paths / URIs of a record. A rename moves records.logical_path
-- and records the old value here; the record id never changes.
CREATE TABLE IF NOT EXISTS record_aliases (
    source_id    TEXT NOT NULL,
    record_id    TEXT NOT NULL,
    alias        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (source_id, alias),
    FOREIGN KEY (source_id, record_id) REFERENCES records(source_id, record_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_record_aliases_record ON record_aliases(source_id, record_id);

-- Immutable revisions. revision_id = derive_revision_id(source, record, digest).
CREATE TABLE IF NOT EXISTS revisions (
    revision_id        TEXT PRIMARY KEY,
    seq                INTEGER NOT NULL UNIQUE,
    source_id          TEXT NOT NULL,
    record_id          TEXT NOT NULL,
    digest             TEXT NOT NULL,
    byte_length        INTEGER NOT NULL CHECK (byte_length >= 0),
    media_type         TEXT NOT NULL,
    observed_at        TEXT NOT NULL,
    upstream_version   TEXT,
    upstream_date      TEXT,
    bytes_retained     INTEGER NOT NULL CHECK (bytes_retained IN (0, 1)),
    unavailable_reason TEXT,
    external_locator   TEXT,
    deleted_at         TEXT,
    CHECK (bytes_retained = 1 OR unavailable_reason IS NOT NULL),
    FOREIGN KEY (source_id, record_id) REFERENCES records(source_id, record_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_revisions_record ON revisions(source_id, record_id, seq);
CREATE INDEX IF NOT EXISTS idx_revisions_digest ON revisions(digest);

-- Retained bytes, separate from the metadata so deletion can drop them alone.
CREATE TABLE IF NOT EXISTS revision_bytes (
    revision_id  TEXT PRIMARY KEY REFERENCES revisions(revision_id) ON DELETE CASCADE,
    content      BLOB NOT NULL
);

-- Evidence spans: typed locator + exact excerpt digest + extraction method.
CREATE TABLE IF NOT EXISTS evidence_spans (
    evidence_id         TEXT PRIMARY KEY,
    revision_id         TEXT NOT NULL REFERENCES revisions(revision_id) ON DELETE CASCADE,
    locator_json        TEXT NOT NULL,
    excerpt_digest      TEXT NOT NULL,
    extraction_method   TEXT NOT NULL,
    extraction_version  TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_spans_revision ON evidence_spans(revision_id);

-- Derivation lineage of module runs (P6 calls the ingestion API).
CREATE TABLE IF NOT EXISTS derivations (
    run_id             TEXT PRIMARY KEY,
    module_id          TEXT NOT NULL,
    module_version     TEXT NOT NULL,
    parameters_digest  TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS derivation_revisions (
    run_id       TEXT NOT NULL REFERENCES derivations(run_id) ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('parent', 'output')),
    revision_id  TEXT NOT NULL,
    position     INTEGER NOT NULL,
    PRIMARY KEY (run_id, role, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_derivation_revisions_revision
    ON derivation_revisions(revision_id, role);

-- Minimal tombstones left by deletion when the policy permits (ADR 0008):
-- identity, digest, deletion time and reason; no bytes, no text.
CREATE TABLE IF NOT EXISTS tombstones (
    source_id    TEXT NOT NULL,
    record_id    TEXT NOT NULL,
    revision_id  TEXT NOT NULL,
    digest       TEXT NOT NULL,
    deleted_at   TEXT NOT NULL,
    reason       TEXT NOT NULL,
    PRIMARY KEY (revision_id)
);
CREATE INDEX IF NOT EXISTS idx_tombstones_record ON tombstones(source_id, record_id);

-- Idempotent ingestion: one row per idempotency key with the digest of its inputs.
CREATE TABLE IF NOT EXISTS ingest_requests (
    idempotency_key  TEXT PRIMARY KEY,
    source_id        TEXT NOT NULL,
    record_id        TEXT NOT NULL,
    request_digest   TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingest_requests_record ON ingest_requests(source_id, record_id);

-- Outbox for cross-store work (FTS/vector index generations live in other
-- databases): written in the same transaction as the evidence change,
-- consumed idempotently and acknowledged by seq.
CREATE TABLE IF NOT EXISTS evidence_outbox (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    event         TEXT NOT NULL CHECK (event IN
        ('revision_added', 'record_renamed', 'record_deleted')),
    source_id     TEXT NOT NULL,
    record_id     TEXT NOT NULL,
    revision_id   TEXT,
    payload_json  TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    processed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_outbox_pending
    ON evidence_outbox(processed_at, seq);
