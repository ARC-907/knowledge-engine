"""Agent Board — provider-credential registry.

The Config-tab surface for *provider-abstracted keys*: a registry that binds
a provider (anthropic / openai / cloud-http / ollama / custom) to the
environment variable that holds its secret. The operator "places" a key
through the GUI by declaring `provider → ENV_VAR`; the actual secret lives
in the process environment and is **never** written to the database.

Why env-var bindings rather than stored secrets:

* Honors the cardinal rule — no secrets in any committed file. The shipped
  SQLite DB (and therefore any backup or repo) contains binding metadata
  only, never a credential.
* Matches how `routing/cloud.py` and the classifier already read keys
  (`KE_ANTHROPIC_API_KEY`, `KE_OPENAI_API_KEY`, `KE_CLOUD_API_KEY`, …), so
  the registry is the single source of truth the engine resolves against
  instead of a parallel island.
* A local-only operator who would rather paste a value can still set the
  env var in their shell / `.env`; the registry then reports it as live.

Ships empty: nothing seeds the `api_keys` table. `list_providers()` reports
whether each binding's env var is currently populated (`env_set`) without
ever returning the secret. `resolve_secret()` returns the live value for
in-process engine use only — it is not exposed through any list/HTTP read.

Distinct from `keys.py` (the board-ACCESS vault: hashed `X-Board-Key`
tokens that authenticate callers *to* the board). This module is about
credentials the engine uses to authenticate *out* to providers.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..foundation import db

# Recommended env-var binding per known provider. Operators may override
# with any name; these are just the defaults the GUI pre-fills and that
# `routing/` already reads.
DEFAULT_ENV_VARS: dict[str, str] = {
    "anthropic": "KE_ANTHROPIC_API_KEY",
    "openai": "KE_OPENAI_API_KEY",
    "cloud-http": "KE_CLOUD_API_KEY",
    "ollama": "",          # local; usually keyless (uses KE_OLLAMA_URL)
    "custom": "",
}

KNOWN_PROVIDERS = tuple(DEFAULT_ENV_VARS.keys())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_env_var(provider: str) -> str:
    """Recommended env-var name for a provider (empty string if keyless)."""
    return DEFAULT_ENV_VARS.get(provider.strip().lower(), "")


# ── CRUD ───────────────────────────────────────────────────────


def register_provider(
    provider: str,
    env_var: str | None = None,
    display_name: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Register (or re-register) a provider → env-var binding.

    `env_var` defaults to the recommended name for the provider. No secret
    is stored — only the binding. Returns the created row (without any
    secret material).
    """
    provider = str(provider or "").strip().lower()
    if not provider:
        raise ValueError("provider is required")
    if len(provider) > 60:
        raise ValueError("provider too long (max 60 chars)")
    env_var = (env_var if env_var is not None else default_env_var(provider)).strip()
    if len(env_var) > 120:
        raise ValueError("env_var too long (max 120 chars)")
    display_name = (display_name or provider).strip()[:120]

    key_id = str(uuid4())
    conn = db.get_connection()
    conn.execute(
        """INSERT INTO api_keys
           (key_id, provider, display_name, env_var, enabled, last_verified, notes)
           VALUES (?, ?, ?, ?, 1, NULL, ?)""",
        (key_id, provider, display_name, env_var, notes),
    )
    conn.commit()
    return get_provider(key_id)  # type: ignore[return-value]


def get_provider(key_id: str) -> dict[str, Any] | None:
    """Fetch one binding by id, annotated with `env_set` (never the secret)."""
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
    if row is None:
        return None
    return _annotate(dict(row))


def list_providers() -> list[dict[str, Any]]:
    """All bindings, each annotated with `env_set`. Never returns secrets."""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT * FROM api_keys ORDER BY provider, display_name"
    ).fetchall()
    return [_annotate(dict(r)) for r in rows]


def toggle_provider(key_id: str) -> dict[str, Any] | None:
    conn = db.get_connection()
    row = conn.execute("SELECT enabled FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
    if row is None:
        return None
    new_state = 0 if row["enabled"] else 1
    conn.execute("UPDATE api_keys SET enabled = ? WHERE key_id = ?", (new_state, key_id))
    conn.commit()
    return get_provider(key_id)


def delete_provider(key_id: str) -> bool:
    conn = db.get_connection()
    cur = conn.execute("DELETE FROM api_keys WHERE key_id = ?", (key_id,))
    conn.commit()
    return cur.rowcount > 0


def verify_provider(key_id: str) -> dict[str, Any] | None:
    """Check whether the binding's env var is currently populated.

    Stamps `last_verified` and returns the annotated row. Does not reveal
    or persist the secret — only that it is present.
    """
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM api_keys WHERE key_id = ?", (key_id,)).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE api_keys SET last_verified = ? WHERE key_id = ?",
        (_now_iso(), key_id),
    )
    conn.commit()
    return get_provider(key_id)


# ── Resolution (in-process engine use only) ────────────────────


def resolve_secret(provider: str) -> str | None:
    """Return the live secret for an enabled provider, read from the env.

    For in-process engine use (routing, classifier). NOT exposed by any
    list/HTTP read. Returns None if the provider is unregistered, disabled,
    keyless, or its env var is unset.
    """
    provider = str(provider or "").strip().lower()
    conn = db.get_connection()
    row = conn.execute(
        "SELECT env_var FROM api_keys WHERE provider = ? AND enabled = 1 LIMIT 1",
        (provider,),
    ).fetchone()
    if row is None:
        return None
    env_var = (row["env_var"] or "").strip()
    if not env_var:
        return None
    val = os.environ.get(env_var)
    return val or None


def resolve_env_var(provider: str) -> str | None:
    """The env-var name bound to an enabled provider (no secret)."""
    provider = str(provider or "").strip().lower()
    conn = db.get_connection()
    row = conn.execute(
        "SELECT env_var FROM api_keys WHERE provider = ? AND enabled = 1 LIMIT 1",
        (provider,),
    ).fetchone()
    if row is None:
        return None
    return (row["env_var"] or "").strip() or None


# ── Helpers ────────────────────────────────────────────────────


def _annotate(row: dict[str, Any]) -> dict[str, Any]:
    """Add `env_set` (is the bound env var populated?) without leaking it."""
    env_var = (row.get("env_var") or "").strip()
    row["env_set"] = bool(env_var and os.environ.get(env_var))
    row["enabled"] = bool(row.get("enabled"))
    # Defensive: there is no secret column, but never echo anything that
    # could be one.
    row.pop("secret", None)
    row.pop("secret_value", None)
    return row


__all__ = [
    "DEFAULT_ENV_VARS",
    "KNOWN_PROVIDERS",
    "default_env_var",
    "register_provider",
    "get_provider",
    "list_providers",
    "toggle_provider",
    "delete_provider",
    "verify_provider",
    "resolve_secret",
    "resolve_env_var",
]
