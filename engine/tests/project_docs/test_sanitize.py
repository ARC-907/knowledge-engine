"""Tests for the project-docs sanitization layer.

All secrets here are synthetic — they exercise the redaction *shapes*, not any
real credential.
"""

from __future__ import annotations

import dataclasses

from knowledge_engine.project_docs import schema
from knowledge_engine.project_docs.config import ProjectDocsConfig
from knowledge_engine.project_docs.sanitize import SanitizeResult, sanitize


def _cfg(**ingestion_overrides: object) -> ProjectDocsConfig:
    """Build a config, optionally overriding nested ingestion fields."""
    base = ProjectDocsConfig()
    if not ingestion_overrides:
        return base
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
