"""Tests for the project-context sanitization layer.

All secrets here are synthetic — they exercise the redaction *shapes*, not any
real credential.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from knowledge_engine.project_context import schema
from knowledge_engine.project_context.config import ProjectContextConfig
from knowledge_engine.project_context.sanitize import SanitizeResult, sanitize


def _cfg(**ingestion_overrides: object) -> ProjectContextConfig:
    """Build a config, optionally overriding nested ingestion fields."""
    base = ProjectContextConfig()
    if not ingestion_overrides:
        return base
    # ``**ingestion_overrides`` is object-typed; replace() validates the fields at runtime.
    new_ingestion = dataclasses.replace(base.ingestion, **ingestion_overrides)  # type: ignore[arg-type]
    return dataclasses.replace(base, ingestion=new_ingestion)


def test_clean_text_is_sanitized_with_zero_redactions() -> None:
    result = sanitize("Just a plain documentation paragraph.", _cfg())
    assert isinstance(result, SanitizeResult)
    assert result.status == schema.SANITIZED
    assert result.redactions == 0
    assert result.text == "Just a plain documentation paragraph."


def test_aws_access_key_redacted_and_counted() -> None:
    text = "key id is AKIAIOSFODNN7EXAMPLE here"
    result = sanitize(text, _cfg())
    assert result.status == schema.SANITIZED
    assert result.redactions == 1
    assert "AKIAIOSFODNN7EXAMPLE" not in result.text
    assert "<REDACTED:AWS_ACCESS_KEY_ID>" in result.text


def test_api_key_assignment_redacted() -> None:
    text = 'SERVICE_API_KEY="sk-supersecretvalue123"'
    result = sanitize(text, _cfg())
    assert result.status == schema.SANITIZED
    assert result.redactions == 1
    assert "sk-supersecretvalue123" not in result.text
    assert result.text.startswith("SERVICE_API_KEY=")


def test_bearer_token_redacted() -> None:
    text = "Authorization: Bearer aaaa.bbbb.cccc"
    result = sanitize(text, _cfg())
    assert result.status == schema.SANITIZED
    assert result.redactions == 1
    assert "aaaa.bbbb.cccc" not in result.text


def test_url_credentials_removed() -> None:
    text = "clone from https://alice:hunter2@example.com/repo.git now"
    result = sanitize(text, _cfg())
    assert result.status == schema.SANITIZED
    assert result.redactions == 1
    assert "alice:hunter2" not in result.text
    assert "hunter2" not in result.text
    assert "https://" in result.text
    assert "@example.com/repo.git" in result.text


def test_home_path_collapsed_to_tilde() -> None:
    text = "logfile at /home/someuser/project/run.log"
    result = sanitize(text, _cfg())
    assert result.status == schema.SANITIZED
    assert result.redactions == 1
    assert "/home/someuser" not in result.text
    assert "~/project/run.log" in result.text


def test_multiple_secrets_increment_count() -> None:
    text = "id AKIAIOSFODNN7EXAMPLE and TOKEN=abc123secret"
    result = sanitize(text, _cfg())
    assert result.status == schema.SANITIZED
    assert result.redactions == 2


def test_oversize_text_rejected() -> None:
    cfg = _cfg(max_document_bytes=16)
    result = sanitize("this string is definitely longer than sixteen bytes", cfg)
    assert result.status == schema.REJECTED_OVERSIZE
    assert result.redactions == 0
    assert result.text == ""


def test_binary_text_rejected() -> None:
    result = sanitize("good start\x00\x01binary tail", _cfg())
    assert result.status == schema.REJECTED_BINARY
    assert result.redactions == 0
    assert result.text == ""


def test_email_not_redacted_by_default() -> None:
    text = "contact dev@example.com for details"
    result = sanitize(text, _cfg())
    assert result.status == schema.SANITIZED
    assert "dev@example.com" in result.text
    assert result.redactions == 0


# ── extended secret shapes ───────────────────────────────────────────
#
# Token-shaped samples are assembled at runtime so the source file never holds a
# literal that looks like a real credential to secret scanners.


def _j(*parts: str) -> str:
    return "".join(parts)


_PEM_BODY = ["MIIEowIBAAKCAQEAsyntheticKeyMaterialLine1abc", "Zm9vYmFyYmF6c3ludGhldGljbGluZTJkZWY="]


def _pem(label: str, *, end: bool = True) -> str:
    lines = [_j("-----BEGIN ", label, "-----"), *_PEM_BODY]
    if end:
        lines.append(_j("-----END ", label, "-----"))
    return "\n".join(lines)


_SK = _j("sk", "-", "Synth3ticKeyValue0123456789abcd")
_SK_PROJ = _j("sk", "-proj-", "Synth3tic_Proj-Key0123456789")
_GH = {kind: _j("gh", kind, "_", "A1b2C3d4E5" * 4) for kind in "pousr"}
_GH_PAT = _j("github", "_pat_", "11ABCDEFG0123456789_synthetic0123456789")
_SLACK = {kind: _j("xo", "x", kind, "-", "1234567890-synthetic") for kind in "baprs"}
_OPAQUE = "SynthOpaqueToken0123456789+/="


@pytest.mark.parametrize(
    ("text", "secret", "marker"),
    [
        (_SK, _SK, "<REDACTED:OPENAI_API_KEY>"),
        (f"OPENAI={_SK_PROJ} next", _SK_PROJ, "<REDACTED:OPENAI_API_KEY>"),
        *[(f"token {tok} end", tok, "<REDACTED:GITHUB_TOKEN>") for tok in _GH.values()],
        (f"pat={_GH_PAT}", _GH_PAT, "<REDACTED:GITHUB_TOKEN>"),
        *[(f"slack {tok} end", tok, "<REDACTED:SLACK_TOKEN>") for tok in _SLACK.values()],
        (f"Authorization: Bearer {_OPAQUE}", _OPAQUE, "Bearer <REDACTED:BEARER_TOKEN>"),
        (f"curl -H 'Authorization: Bearer   {_OPAQUE}'", _OPAQUE, "<REDACTED:BEARER_TOKEN>"),
    ],
)
def test_provider_tokens_redacted(text: str, secret: str, marker: str) -> None:
    result = sanitize(text, _cfg())
    assert result.status == schema.SANITIZED
    assert secret not in result.text
    assert marker in result.text
    assert result.redactions == 1


@pytest.mark.parametrize(
    "label",
    [
        "PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "PGP PRIVATE KEY BLOCK",
    ],
)
def test_pem_private_key_block_redacted(label: str) -> None:
    text = f"before\n{_pem(label)}\nafter"
    result = sanitize(text, _cfg())
    assert result.text == "before\n<REDACTED:PRIVATE_KEY>\nafter"
    assert result.redactions == 1


def test_truncated_pem_private_key_redacted() -> None:
    text = f"log line\n{_pem('RSA PRIVATE KEY', end=False)}\n\nsome prose after."
    result = sanitize(text, _cfg())
    for line in _PEM_BODY:
        assert line not in result.text
    assert "BEGIN" not in result.text
    assert result.text.startswith("log line\n<REDACTED:PRIVATE_KEY>")
    assert result.text.endswith("some prose after.")


def test_two_pem_blocks_redacted_separately() -> None:
    text = f"{_pem('PRIVATE KEY')}\nmiddle text\n{_pem('EC PRIVATE KEY')}"
    result = sanitize(text, _cfg())
    assert result.text == "<REDACTED:PRIVATE_KEY>\nmiddle text\n<REDACTED:PRIVATE_KEY>"
    assert result.redactions == 2


@pytest.mark.parametrize(
    "text",
    [
        _j(
            "-----BEGIN ",
            "PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A\n-----END PUBLIC KEY-----",
        ),
        _j(
            "-----BEGIN ",
            "CERTIFICATE-----\nMIIDdzCCAl+gAwIBAgIEAgAAuTANBgkq\n-----END CERTIFICATE-----",
        ),
        "uses sk-learn for the model",
        "ghp_short",
        "xoxb-short",
        "Bearer token",
        "the Bearer of this note",
        '{"name": "value", "count": "3"}',
        '{"token_count": 5}',
    ],
)
def test_non_secrets_left_alone(text: str) -> None:
    result = sanitize(text, _cfg())
    assert result.text == text
    assert result.redactions == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"api_key": "abc123xyz"}', '{"api_key": "<REDACTED:SECRET>"}'),
        ('{"API-KEY":"abc123xyz", "n": 1}', '{"API-KEY":"<REDACTED:SECRET>", "n": 1}'),
        ('{"client_secret" : "p@ss w0rd"}', '{"client_secret" : "<REDACTED:SECRET>"}'),
        ('{"access_token": "t0k"}', '{"access_token": "<REDACTED:SECRET>"}'),
        ('{"password": "ab\\"cd"}', '{"password": "<REDACTED:SECRET>"}'),
        ('{"passwd": "x"}', '{"passwd": "<REDACTED:SECRET>"}'),
        (
            '{"Authorization": "Basic dXNlcjpwYXNz"}',
            '{"Authorization": "<REDACTED:SECRET>"}',
        ),
        ("{'token': 'py-dict-value'}", "{'token': '<REDACTED:SECRET>'}"),
        ("{'secret': \"mixed\"}", "{'secret': \"<REDACTED:SECRET>\"}"),
    ],
)
def test_quoted_key_value_pairs_redacted(text: str, expected: str) -> None:
    result = sanitize(text, _cfg())
    assert result.text == expected
    assert result.redactions == 1


def test_quoted_pair_with_token_value_counts_once() -> None:
    result = sanitize(f'{{"api_key": "{_SK}"}}', _cfg())
    assert result.text == '{"api_key": "<REDACTED:OPENAI_API_KEY>"}'
    assert result.redactions == 1


def test_env_export_of_secret_name_counts_once() -> None:
    result = sanitize("export API_TOKEN=abc123", _cfg())
    assert "abc123" not in result.text
    assert result.redactions == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # YAML / INI multi-line scalars: the value sits on the line after the separator.
        ("api_token:\n  s3cr3tValue123", "api_token:\n  <REDACTED:SECRET>"),
        ("password:\nhunter2", "password:\n<REDACTED:SECRET>"),
        ("DB_PASSWORD =\n  hunter2", "DB_PASSWORD =\n  <REDACTED:SECRET>"),
        ("secret:\r\n\tcrlfValue9 next", "secret:\r\n\t<REDACTED:SECRET> next"),
        ("token:\n  'quotedNextLine' rest", "token:\n  '<REDACTED:SECRET>' rest"),
    ],
)
def test_assignment_value_on_next_line_redacted(text: str, expected: str) -> None:
    """Regression: a value on the line after ``key:`` / ``key=`` is still a secret."""
    result = sanitize(text, _cfg())
    assert result.text == expected
    assert result.redactions == 1


def test_assignment_separator_does_not_span_blank_lines() -> None:
    """A blank line ends the assignment; the paragraph after it is not a value."""
    text = "password:\n\nThis paragraph is prose."
    result = sanitize(text, _cfg())
    assert result.text == text
    assert result.redactions == 0


def test_assignment_on_next_line_does_not_swallow_next_key() -> None:
    """``TOKEN=`` followed by another key on the next line redacts that key's value."""
    result = sanitize("TOKEN=\ndb_token: yamlSecret4", _cfg())
    assert "yamlSecret4" not in result.text
    assert "db_token:" in result.text


# ── idempotency ──────────────────────────────────────────────────────

_SAMPLES: list[tuple[str, str]] = [
    ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    (_SK, _SK),
    (_SK_PROJ, _SK_PROJ),
    (_GH["p"], _GH["p"]),
    (_GH_PAT, _GH_PAT),
    (_SLACK["b"], _SLACK["b"]),
    (f"Bearer {_OPAQUE}", _OPAQUE),
    ("Bearer aaaa.bbbb.cccc", "aaaa.bbbb.cccc"),
    ('"api_key": "jsonSecretValue1"', "jsonSecretValue1"),
    ("{'password': 'pyDictSecret2'}", "pyDictSecret2"),
    ("PASSWORD=assignSecret3", "assignSecret3"),
    ("db_token: yamlSecret4", "yamlSecret4"),
    ("api_token:\n  multiLineSecret7", "multiLineSecret7"),
    ("https://alice:urlSecret5@example.com/repo.git", "urlSecret5"),
    ("export SOME_VAR=envSecret6", "envSecret6"),
    (_pem("RSA PRIVATE KEY"), _PEM_BODY[0]),
    (_pem("OPENSSH PRIVATE KEY", end=False), _PEM_BODY[1]),
]

# Fragments that stress rule interplay: half-built assignments, markers, paths.
_TRICKY = [
    "TOKEN=",
    "password:",
    '"secret": "',
    "'token': '",
    "export",
    "export X=",
    "Bearer",
    "<REDACTED:SECRET>",
    "<REDACTED:",
    "/home/",
    "/home/someuser/",
    "/Users/x",
    "C:\\Users\\",
    "https://",
    "user:pw@",
    "@",
    "=",
    ":",
    '"',
    "'",
    "~",
    "-----BEGIN PRIVATE KEY-----",
    "-----END PRIVATE KEY-----",
    "sk-",
    "ghp_",
]

_SEPARATORS = [" ", "\n", "\t", " | ", ", ", "; ", "\r\n"]

_fragments = st.one_of(
    st.sampled_from([text for text, _ in _SAMPLES]),
    st.sampled_from(_TRICKY),
    st.text(st.characters(blacklist_categories=("Cs",)), max_size=16),
)


@st.composite
def _documents(draw: st.DrawFn) -> str:
    parts = draw(st.lists(_fragments, max_size=10))
    out = ""
    for part in parts:
        out += part + draw(st.sampled_from(_SEPARATORS))
    return out


# Deterministic and bounded so the suite stays fast and reproducible; the
# strategy above mixes real token shapes with the fragments most likely to
# interact badly, so 200 examples cover the rule interplay well.
_PROPERTY_SETTINGS = settings(
    max_examples=200,
    derandomize=True,
    database=None,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


@pytest.mark.parametrize(("text", "secret"), _SAMPLES)
def test_samples_redacted_and_stable(text: str, secret: str) -> None:
    first = sanitize(text, _cfg())
    assert secret not in first.text
    assert first.redactions >= 1
    second = sanitize(first.text, _cfg())
    assert second.text == first.text
    assert second.redactions == 0


@_PROPERTY_SETTINGS
@given(_documents())
def test_sanitize_is_idempotent(doc: str) -> None:
    """``sanitize`` is a fixed point on its own output, with nothing left to redact.

    A rejected input (binary or oversize) yields empty text, which then
    re-sanitizes cleanly, so the first status is only meaningful when the input
    was accepted; the second pass must always be a clean no-op.
    """
    once = sanitize(doc, _cfg())
    twice = sanitize(once.text, _cfg())
    assert twice.text == once.text
    assert twice.status == schema.SANITIZED
    assert twice.redactions == 0
    if once.status != schema.SANITIZED:
        assert once.text == "" and once.redactions == 0


@_PROPERTY_SETTINGS
@given(_documents())
def test_no_sample_secret_survives(doc: str) -> None:
    out = sanitize(doc, _cfg()).text
    for text, secret in _SAMPLES:
        if text in doc:
            assert secret not in out, (text, out)


# ``redact_emails`` is an optional field that sanitize() reads with getattr.
_EMAIL_CFG = SimpleNamespace(
    ingestion=SimpleNamespace(
        max_document_bytes=ProjectContextConfig().ingestion.max_document_bytes,
        redact_emails=True,
    )
)


@_PROPERTY_SETTINGS
@given(_documents())
def test_sanitize_is_idempotent_with_email_redaction(doc: str) -> None:
    once = sanitize(doc, _EMAIL_CFG)  # type: ignore[arg-type]
    twice = sanitize(once.text, _EMAIL_CFG)  # type: ignore[arg-type]
    assert twice.text == once.text
    assert twice.redactions == 0
