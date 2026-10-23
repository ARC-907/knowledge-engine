"""Sanitization strategy for the project-context subsystem.

Before any externally-sourced text (documents, docstrings, logs, git output) is
written to a project store, it passes through :func:`sanitize`. The goal is
conservative, default-safe redaction of synthetic-secret patterns and
machine-specific paths, plus hard rejection of binary or oversized inputs.

The rule set is an ordered list of :class:`Rule` records (``name``, compiled
``pattern``, ``replacement``) so it stays extensible and individually testable.
All status values come from :mod:`knowledge_engine.project_context.schema` — never
raw string literals — so the controlled vocabulary cannot drift.

:func:`sanitize` is idempotent: rules never re-match an existing
``<REDACTED:...>`` marker, and the rule set is re-applied until the text stops
changing. Sanitizing already-sanitized text is a no-op with zero redactions.

This module is pure: it performs no I/O, no subprocess calls, and no network
access. ``retain_raw_content`` is the *caller's* concern; :func:`sanitize` only
produces the sanitized text plus a status and a redaction count.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from knowledge_engine.project_context import schema
from knowledge_engine.project_context.config import ProjectContextConfig

logger = logging.getLogger(__name__)


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


#: Negative lookahead placed where a rule's secret value starts, so a value that
#: is already a redaction marker is never matched (and counted) again.
_NOT_REDACTED = r"(?!<REDACTED:)"

#: Safety cap on whole-rule-set passes; real inputs settle in one or two.
_MAX_PASSES = 4

#: Key names whose values are treated as secrets by the assignment rules.
_CREDENTIAL_NAME = r"[\w.\-]*(?:api[_\-]?key|secret|token|password|passwd|pwd)"

#: Refuses a "value" that is really the next secret key (``TOKEN= db_token: x``),
#: so one assignment can never swallow the key of the next and leave its value.
_NOT_A_CREDENTIAL_NAME = rf"(?!{_CREDENTIAL_NAME}\b[ \t]*[:=])"

_PEM_LABEL = r"[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?"

# Ordered redaction rules. Order matters: whole PEM blocks go first, then
# specific token shapes (URL credentials, provider keys, Bearer tokens), and
# only then the generic key/value assignment rules, so the more specific masks
# win and the generic rules skip values that are already redacted.
_RULES: tuple[Rule, ...] = (
    # PEM private keys (RSA/EC/DSA/OPENSSH/ENCRYPTED/PKCS#8, PGP private key
    # blocks). A complete BEGIN...END block is replaced as one unit; a block
    # with no END marker (truncated output) is redacted through its trailing
    # base64 / header lines.
    Rule(
        name="private_key",
        pattern=re.compile(
            rf"-----BEGIN {_PEM_LABEL}-----"
            rf"(?:.*?-----END {_PEM_LABEL}-----"
            r"|(?:[ \t]*\r?\n[ \t]*(?:(?:Proc-Type|DEK-Info|Version|Comment):[^\r\n]*"
            r"|[A-Za-z0-9+/=]{16,}))*)",
            re.DOTALL,
        ),
        replacement=_placeholder("private_key"),
    ),
    # AWS-style access key IDs (synthetic shape only).
    Rule(
        name="aws_access_key_id",
        pattern=re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA)[0-9A-Z]{16}\b"),
        replacement=_placeholder("aws_access_key_id"),
    ),
    # Credentials embedded in URLs: scheme://user:pass@host -> scheme://<REDACTED>@host
    Rule(
        name="url_credentials",
        pattern=re.compile(
            r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)" + _NOT_REDACTED + r"[^/\s:@]+:[^/\s:@]+@"
        ),
        replacement=r"\g<scheme>" + _placeholder("url_credentials") + "@",
    ),
    # OpenAI-style secret keys (sk-..., sk-proj-..., and other sk- prefixed keys).
    Rule(
        name="openai_api_key",
        pattern=re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
        replacement=_placeholder("openai_api_key"),
    ),
    # GitHub tokens: classic/OAuth/user/server/refresh (gh[pousr]_) and
    # fine-grained personal access tokens (github_pat_).
    Rule(
        name="github_token",
        pattern=re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
        replacement=_placeholder("github_token"),
    ),
    # Slack tokens (bot, app-level user, refresh ...).
    Rule(
        name="slack_token",
        pattern=re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
        replacement=_placeholder("slack_token"),
    ),
    # "Bearer <jwt>" authorization headers.
    Rule(
        name="bearer_token",
        pattern=re.compile(
            r"\bBearer\s+[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+",
        ),
        replacement="Bearer " + _placeholder("bearer_token"),
    ),
    # "Bearer <opaque token>" (any RFC 6750 token68 of 16+ characters).
    Rule(
        name="bearer_opaque",
        pattern=re.compile(r"\bBearer\s+" + _NOT_REDACTED + r"[A-Za-z0-9._~+/=\-]{16,}"),
        replacement="Bearer " + _placeholder("bearer_token"),
    ),
    # JSON / Python-literal quoted pairs: "api_key": "value" or 'password': 'value'.
    # The quote before the separator defeats the word-boundary assignment rule
    # below, so these get their own rule. Keeps the key, redacts the value.
    # The closing value quote is matched by lookahead, not consumed, so it can
    # still open the next quoted key on the same line.
    Rule(
        name="quoted_secret_assignment",
        pattern=re.compile(
            r"(?P<kq>[\"'])"
            r"(?P<key>[\w.\-]*(?:api[_\-]?key|secret|token|password|passwd|pwd|authorization))"
            r"(?P=kq)"
            r"(?P<sep>\s*:\s*)"
            r"(?P<vq>[\"'])" + _NOT_REDACTED + r"(?:\\.|(?!(?P=vq))[^\\\r\n])+(?=(?P=vq))",
            re.IGNORECASE,
        ),
        replacement=r"\g<kq>\g<key>\g<kq>\g<sep>\g<vq>" + _placeholder("secret"),
    ),
    # Shell exports (export FOO=bar). Runs before the generic assignment rule so
    # a preceding "token: export" cannot consume the ``export`` keyword.
    Rule(
        name="env_assignment",
        pattern=re.compile(
            r"(?P<prefix>\bexport\s+)(?P<name>[A-Z][A-Z0-9_]*)(?P<sep>=)(?P<quote>['\"]?)"
            + _NOT_REDACTED
            + r"[^\s'\"]+(?=(?P=quote))",
        ),
        replacement=r"\g<prefix>\g<name>\g<sep>\g<quote>" + _placeholder("env"),
    ),
    # Generic NAME_API_KEY= / token= / secret= / password= assignments. Matches an
    # optional quote and the value up to the next whitespace/quote. The
    # separator stays on one line and the value may not itself be a secret key.
    Rule(
        name="secret_assignment",
        pattern=re.compile(
            rf"(?P<key>\b{_CREDENTIAL_NAME}\b)"
            r"(?P<sep>[ \t]*[:=][ \t]*)"
            r"(?P<quote>['\"]?)" + _NOT_REDACTED + _NOT_A_CREDENTIAL_NAME + r"[^\s'\"]+"
            r"(?=(?P=quote))",
            re.IGNORECASE,
        ),
        replacement=r"\g<key>\g<sep>\g<quote>" + _placeholder("secret"),
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


def _redact_emails_enabled(cfg: ProjectContextConfig) -> bool:
    """Email redaction is opt-in; tolerate configs that lack the field."""
    return bool(getattr(cfg.ingestion, "redact_emails", False))


def sanitize(text: str, cfg: ProjectContextConfig, *, content_kind: str = "doc") -> SanitizeResult:
    """Sanitize ``text`` according to the conservative project-context rules.

    The decision order is: reject binary, reject oversize, then apply redaction
    rules in order and count masked matches. The rule set is re-applied (up to
    a small cap) until a pass changes nothing, so the result is a fixed point:
    ``sanitize(sanitize(x).text).text == sanitize(x).text``.

    Args:
        text: The raw text to sanitize.
        cfg: The active project-context config (provides the oversize limit and the
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

    rules = (*_RULES, _EMAIL_RULE) if _redact_emails_enabled(cfg) else _RULES

    redactions = 0
    out = text
    for _ in range(_MAX_PASSES):
        before = out
        for rule in rules:
            out, count = rule.pattern.subn(rule.replacement, out)
            redactions += count
        if out == before:
            break
    else:
        logger.debug("sanitize: text still changing after %d passes", _MAX_PASSES)

    return SanitizeResult(text=out, status=schema.SANITIZED, redactions=redactions)
