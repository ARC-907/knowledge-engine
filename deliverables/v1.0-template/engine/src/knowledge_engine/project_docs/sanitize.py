"""Sanitization strategy for the project-docs subsystem.

Before any externally-sourced text (documents, docstrings, logs, git output) is
written to a project store, it passes through :func:`sanitize`. The goal is
conservative, default-safe redaction of synthetic-secret patterns and
machine-specific paths, plus hard rejection of binary or oversized inputs.

The rule set is an ordered list of :class:`Rule` records (``name``, compiled
``pattern``, ``replacement``) so it stays extensible and individually testable.
All status values come from :mod:`knowledge_engine.project_docs.schema` — never
raw string literals — so the controlled vocabulary cannot drift.

This module is pure: it performs no I/O, no subprocess calls, and no network
access. ``retain_raw_content`` is the *caller's* concern; :func:`sanitize` only
produces the sanitized text plus a status and a redaction count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge_engine.project_docs import schema
from knowledge_engine.project_docs.config import ProjectDocsConfig


@dataclass
class SanitizeResult:
    """Outcome of sanitizing one blob of text.

    Attributes:
        text: The sanitized text (empty for rejected inputs).
        status: A :mod:`schema` sanitization-state constant.
        redactions: How many secret/path matches were masked.
    """

    text: str
    status: str
    redactions: int


@dataclass(frozen=True)
class Rule:
    """A single redaction rule: name, compiled regex, and replacement template."""

    name: str
    pattern: re.Pattern[str]
    replacement: str


def _placeholder(name: str) -> str:
    """Build a stable, human-legible redaction marker for a rule."""
    return f"<REDACTED:{name.upper()}>"


# Ordered redaction rules. Order matters: URL credentials and Bearer tokens are
# handled before the generic assignment rule so the more specific masks win.
_RULES: tuple[Rule, ...] = (
    # AWS-style access key IDs (synthetic shape only).
    Rule(
        name="aws_access_key_id",
        pattern=re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA)[0-9A-Z]{16}\b"),
        replacement=_placeholder("aws_access_key_id"),
    ),
    # Credentials embedded in URLs: scheme://user:pass@host -> scheme://<REDACTED>@host
    Rule(
        name="url_credentials",
        pattern=re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@]+:[^/\s:@]+@"),
        replacement=r"\g<scheme>" + _placeholder("url_credentials") + "@",
    ),
    # "Bearer <jwt>" authorization headers.
    Rule(
        name="bearer_token",
        pattern=re.compile(
            r"\bBearer\s+[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+",
        ),
        replacement="Bearer " + _placeholder("bearer_token"),
    ),
    # Generic NAME_API_KEY= / token= / secret= / password= assignments. Matches an
    # optional quote and the value up to the next whitespace/quote.
    Rule(
        name="secret_assignment",
        pattern=re.compile(
            r"(?P<key>\b[\w.\-]*(?:api[_\-]?key|secret|token|password|passwd|pwd)\b)"
            r"(?P<sep>\s*[:=]\s*)"
            r"(?P<quote>['\"]?)"
            r"[^\s'\"]+"
            r"(?P=quote)",
            re.IGNORECASE,
        ),
        replacement=r"\g<key>\g<sep>\g<quote>" + _placeholder("secret") + r"\g<quote>",
    ),
    # Absolute home paths -> "~". POSIX: /home/<user>, /Users/<user>.
    Rule(
        name="home_path_posix",
        pattern=re.compile(r"/(?:home|Users)/[^/\s:]+"),
        replacement="~",
    ),
    # Absolute home paths -> "~". Windows: C:\Users\<user>.
    Rule(
        name="home_path_windows",
        pattern=re.compile(r"[A-Za-z]:\\Users\\[^\\/\s:]+", re.IGNORECASE),
        replacement="~",
    ),
    # Generic environment-variable assignments exporting a value (export FOO=bar).
    Rule(
        name="env_assignment",
        pattern=re.compile(
            r"(?P<prefix>\bexport\s+)(?P<name>[A-Z][A-Z0-9_]*)(?P<sep>=)(?P<quote>['\"]?)[^\s'\"]+(?P=quote)",
        ),
        replacement=r"\g<prefix>\g<name>\g<sep>\g<quote>" + _placeholder("env") + r"\g<quote>",
    ),
)

# Optional email redaction (enabled via cfg.ingestion.redact_emails when present).
_EMAIL_RULE = Rule(
    name="email",
    pattern=re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    replacement=_placeholder("email"),
)


def _is_binary(text: str) -> bool:
    """Heuristic: text containing a NUL byte is treated as binary/non-text."""
    return "\x00" in text


def _byte_length(text: str) -> int:
    """UTF-8 byte length, used for the oversize gate."""
    return len(text.encode("utf-8", errors="surrogatepass"))


def _redact_emails_enabled(cfg: ProjectDocsConfig) -> bool:
    """Email redaction is opt-in; tolerate configs that lack the field."""
    return bool(getattr(cfg.ingestion, "redact_emails", False))


def sanitize(text: str, cfg: ProjectDocsConfig, *, content_kind: str = "doc") -> SanitizeResult:
    """Sanitize ``text`` according to the conservative project-docs rules.

    The decision order is: reject binary, reject oversize, then apply redaction
    rules in order and count masked matches.

    Args:
        text: The raw text to sanitize.
        cfg: The active project-docs config (provides the oversize limit and the
            optional email-redaction gate).
        content_kind: A free-form label for the source kind (e.g. ``"doc"``,
            ``"docstring"``, ``"log"``). Accepted for caller context; the current
            rule set is kind-agnostic.

    Returns:
        A :class:`SanitizeResult`. Rejected inputs carry an empty ``text`` and a
        ``REJECTED_*`` status with ``redactions == 0``. Clean text returns the
        :data:`schema.SANITIZED` status with ``redactions == 0``.
    """
    del content_kind  # reserved for future per-kind rule selection

    if _is_binary(text):
        return SanitizeResult(text="", status=schema.REJECTED_BINARY, redactions=0)

    if _byte_length(text) > cfg.ingestion.max_document_bytes:
        return SanitizeResult(text="", status=schema.REJECTED_OVERSIZE, redactions=0)

    redactions = 0
    out = text
    for rule in _RULES:
        out, count = rule.pattern.subn(rule.replacement, out)
        redactions += count

    if _redact_emails_enabled(cfg):
        out, count = _EMAIL_RULE.pattern.subn(_EMAIL_RULE.replacement, out)
        redactions += count

    return SanitizeResult(text=out, status=schema.SANITIZED, redactions=redactions)
