"""Controlled vocabularies for the project-docs subsystem.

These are plain ``str`` constants grouped into ``frozenset``s so callers can both
reference a canonical value (``CATEGORY_DOC``) and validate membership
(``"doc" in CATEGORIES``). Keeping them here avoids stringly-typed drift across
the ingestion pipeline, scanner, and MCP tools.
"""

from __future__ import annotations

# ── Document categories ──────────────────────────────────────────────
CATEGORY_DOC = "doc"
CATEGORY_DEVLOG = "devlog"
CATEGORY_QA = "qa"
CATEGORY_DESIGN_NOTE = "design_note"
CATEGORY_DECISION_RECORD = "decision_record"
CATEGORY_TEST_PLAN = "test_plan"
CATEGORY_TEST_LOG = "test_log"
CATEGORY_BUILD_LOG = "build_log"
CATEGORY_RUNTIME_LOG = "runtime_log"
CATEGORY_DIAGNOSTIC_LOG = "diagnostic_log"
CATEGORY_DOCSTRING = "docstring"
CATEGORY_COMMENT = "comment"
CATEGORY_GIT_META = "git_meta"
CATEGORY_DIFF_SUMMARY = "diff_summary"
CATEGORY_SKILL = "skill"
CATEGORY_AGENT_DEF = "agent_def"
CATEGORY_TOOL_DEF = "tool_def"
CATEGORY_REFERENCE = "reference"

CATEGORIES = frozenset({
    CATEGORY_DOC, CATEGORY_DEVLOG, CATEGORY_QA, CATEGORY_DESIGN_NOTE,
    CATEGORY_DECISION_RECORD, CATEGORY_TEST_PLAN, CATEGORY_TEST_LOG,
    CATEGORY_BUILD_LOG, CATEGORY_RUNTIME_LOG, CATEGORY_DIAGNOSTIC_LOG,
    CATEGORY_DOCSTRING, CATEGORY_COMMENT, CATEGORY_GIT_META,
    CATEGORY_DIFF_SUMMARY, CATEGORY_SKILL, CATEGORY_AGENT_DEF,
    CATEGORY_TOOL_DEF, CATEGORY_REFERENCE,
})

# Categories that represent logs (raw retention is extra-sensitive for these).
LOG_CATEGORIES = frozenset({
    CATEGORY_TEST_LOG, CATEGORY_BUILD_LOG,
    CATEGORY_RUNTIME_LOG, CATEGORY_DIAGNOSTIC_LOG,
})

# ── Sanitization outcomes ────────────────────────────────────────────
SANITIZED = "sanitized"
RAW_OMITTED = "raw_omitted"
RAW_RETAINED = "raw_retained"
REDACTED = "redacted"
REJECTED_OVERSIZE = "rejected_oversize"
REJECTED_BINARY = "rejected_binary"
REJECTED_UNSAFE = "rejected_unsafe"

SANITIZATION_STATES = frozenset({
    SANITIZED, RAW_OMITTED, RAW_RETAINED, REDACTED,
    REJECTED_OVERSIZE, REJECTED_BINARY, REJECTED_UNSAFE,
})

# A record whose sanitization status is one of these must not be stored.
REJECTED_STATES = frozenset({
    REJECTED_OVERSIZE, REJECTED_BINARY, REJECTED_UNSAFE,
})

# ── Ingestion outcomes ───────────────────────────────────────────────
INGESTED = "ingested"
SKIPPED_DEDUPE = "skipped_dedupe"
REJECTED = "rejected"
PENDING = "pending"

INGESTION_STATES = frozenset({INGESTED, SKIPPED_DEDUPE, REJECTED, PENDING})

# ── Test/build classifications ───────────────────────────────────────
PASS = "pass"
FAIL = "fail"
ERROR = "error"
UNKNOWN = "unknown"

TEST_CLASSIFICATIONS = frozenset({PASS, FAIL, ERROR, UNKNOWN})

# ── Record link types ────────────────────────────────────────────────
LINK_PARENT = "parent"
LINK_CHILD = "child"
LINK_RELATED = "related"
LINK_CODE_SPAN = "code_span"

LINK_TYPES = frozenset({LINK_PARENT, LINK_CHILD, LINK_RELATED, LINK_CODE_SPAN})

# ── Pointer types ────────────────────────────────────────────────────
POINTER_DOCSTRING = "docstring"
POINTER_DOC = "doc"
POINTER_TESTLOG = "testlog"
POINTER_BUILDLOG = "buildlog"
POINTER_NOTE = "note"

POINTER_TYPES = frozenset({
    POINTER_DOCSTRING, POINTER_DOC, POINTER_TESTLOG,
    POINTER_BUILDLOG, POINTER_NOTE,
})

# ── Scanner modes ────────────────────────────────────────────────────
MODE_REPORT = "report"
MODE_INGEST = "ingest"
MODE_POINTER_PLAN = "pointer_plan"
MODE_POINTER_APPLY = "pointer_apply"
MODE_EMBEDDING = "embedding"

SCAN_MODES = frozenset({
    MODE_REPORT, MODE_INGEST, MODE_POINTER_PLAN,
    MODE_POINTER_APPLY, MODE_EMBEDDING,
})

# ── MCP result modes ─────────────────────────────────────────────────
RESULT_SUMMARY = "summary"
RESULT_FULL = "full"
RESULT_MODES = frozenset({RESULT_SUMMARY, RESULT_FULL})
