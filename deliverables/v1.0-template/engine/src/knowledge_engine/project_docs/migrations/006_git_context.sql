-- Migration 006 — Optional git context. Full diffs are intentionally NOT
-- created here; the git_diffs table is created lazily only when
-- git.include_full_diffs is enabled (see git_context.py).

CREATE TABLE IF NOT EXISTS git_context (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_fp  TEXT NOT NULL,
    branch_fp   TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    branch      TEXT,
    commit_hash TEXT,
    dirty       INTEGER NOT NULL DEFAULT 0,
    remote_hash TEXT,
    data_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_gitctx_branch ON git_context(branch_fp);

CREATE TABLE IF NOT EXISTS git_commits (
    commit_hash  TEXT PRIMARY KEY,
    project_fp   TEXT NOT NULL,
    author_hash  TEXT,
    committed_at TEXT,
    subject      TEXT,
    body_summary TEXT
);

CREATE TABLE IF NOT EXISTS git_diff_summaries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_fp    TEXT NOT NULL,
    from_ref      TEXT,
    to_ref        TEXT,
    files_changed INTEGER NOT NULL DEFAULT 0,
    insertions    INTEGER NOT NULL DEFAULT 0,
    deletions     INTEGER NOT NULL DEFAULT 0,
    summary       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
