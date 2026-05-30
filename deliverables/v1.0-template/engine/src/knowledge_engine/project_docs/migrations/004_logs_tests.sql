-- Migration 004 — Tests and logs as first-class documentation records.

CREATE TABLE IF NOT EXISTS test_runs (
    id             TEXT PRIMARY KEY,
    project_fp     TEXT NOT NULL,
    branch_fp      TEXT NOT NULL,
    command        TEXT NOT NULL DEFAULT '',
    framework      TEXT,
    target         TEXT,
    exit_code      INTEGER,
    classification TEXT NOT NULL DEFAULT 'unknown',
    started_at     TEXT NOT NULL,
    duration_ms    INTEGER,
    git_commit     TEXT,
    git_dirty_json TEXT,
    summary        TEXT NOT NULL DEFAULT '',
    failure_summary TEXT NOT NULL DEFAULT '',
    raw_retained   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_testruns_branch ON test_runs(branch_fp);
CREATE INDEX IF NOT EXISTS idx_testruns_class ON test_runs(classification);
CREATE INDEX IF NOT EXISTS idx_testruns_started ON test_runs(started_at);

CREATE TABLE IF NOT EXISTS test_log_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    test_run_id   TEXT NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    record_id     TEXT,
    sanitized_log TEXT NOT NULL DEFAULT '',
    raw_log       TEXT
);
CREATE INDEX IF NOT EXISTS idx_testlogs_run ON test_log_records(test_run_id);

CREATE TABLE IF NOT EXISTS build_log_records (
    id             TEXT PRIMARY KEY,
    project_fp     TEXT NOT NULL,
    branch_fp      TEXT NOT NULL,
    command        TEXT NOT NULL DEFAULT '',
    exit_code      INTEGER,
    classification TEXT NOT NULL DEFAULT 'unknown',
    started_at     TEXT NOT NULL,
    duration_ms    INTEGER,
    git_commit     TEXT,
    summary        TEXT NOT NULL DEFAULT '',
    sanitized_log  TEXT NOT NULL DEFAULT '',
    raw_log        TEXT,
    raw_retained   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_buildlogs_branch ON build_log_records(branch_fp);

CREATE TABLE IF NOT EXISTS runtime_log_records (
    id             TEXT PRIMARY KEY,
    project_fp     TEXT NOT NULL,
    branch_fp      TEXT NOT NULL,
    command        TEXT NOT NULL DEFAULT '',
    exit_code      INTEGER,
    classification TEXT NOT NULL DEFAULT 'unknown',
    started_at     TEXT NOT NULL,
    duration_ms    INTEGER,
    git_commit     TEXT,
    summary        TEXT NOT NULL DEFAULT '',
    sanitized_log  TEXT NOT NULL DEFAULT '',
    raw_log        TEXT,
    raw_retained   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runtimelogs_branch ON runtime_log_records(branch_fp);
