"""Resolve the corpus embedding provider and index location from :class:`Settings`.

==========================  =============================================  ==================
Setting                     Meaning                                        Default
==========================  =============================================  ==================
``KE_EMBED_PROVIDER``       ``ollama`` / ``remote`` / ``fake``             ``ollama``
``KE_EMBED_MODEL``          model name (recorded in the index)             ``bge-m3:latest``
``KE_EMBED_BATCH``          texts per embedding request                    ``32``
``KE_OLLAMA_URL``           Ollama server of the ``ollama`` provider       ``http://127.0.0.1:11434``
``KE_EMBED_REMOTE_URL``     endpoint of the ``remote`` provider            *(empty)*
``KE_EMBED_ALLOW_REMOTE``   opt-in gate of the ``remote`` provider         off
``KE_EMBEDDINGS_DB``        index database                                 ``<data_dir>/embeddings.db``
==========================  =============================================  ==================

Explicit arguments (the CLI's ``--provider`` / ``--model`` / ``--batch`` /
``--db``) win over the settings. Nothing here performs I/O.
"""

from __future__ import annotations

from pathlib import Path

from ...core.settings import EMBED_PROVIDERS, Settings
from ...project_context.embeddings.providers import EmbeddingProvider, FakeProvider
from .errors import EmbeddingUnavailableError

#: File name of the corpus embedding index inside the data directory.
EMBEDDINGS_FILENAME = "embeddings.db"
#: Seconds allowed for one embedding request to Ollama (the corpus builder's historical value).
EMBED_TIMEOUT = 60.0


def default_db_path(settings: Settings) -> Path:
    """``KE_EMBEDDINGS_DB`` when set, else ``<data_dir>/embeddings.db`` (absolute)."""
    raw = (settings.embeddings_db or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return settings.resolve_paths(warn_missing_corpus=False).data_dir / EMBEDDINGS_FILENAME


def build_provider(
    settings: Settings,
    *,
    provider: str | None = None,
    model: str | None = None,
    batch_size: int | None = None,
) -> EmbeddingProvider:
    """The embedding provider named by ``provider`` (default ``KE_EMBED_PROVIDER``).

    Raises :class:`EmbeddingUnavailableError` for an unknown provider name or
    a ``remote`` provider that is not allowed / has no endpoint.
    """
    name = (provider or settings.embed_provider or "ollama").strip().lower()
    chosen_model = (model or settings.embed_model).strip()
    batch = batch_size if batch_size is not None else settings.embed_batch
    if batch < 1:
        raise ValueError("batch size must be a positive integer")
    if name not in EMBED_PROVIDERS:
        raise EmbeddingUnavailableError(
            f"unknown embedding provider {name!r}",
            hint=f"use one of: {', '.join(EMBED_PROVIDERS)} (KE_EMBED_PROVIDER or --provider)",
            details={"provider": name},
        )
    if name == "fake":
        return FakeProvider()
    if name == "ollama":
        # Imported here: local.py subclasses the provider ABC (keeps import order simple).
        from ...project_context.embeddings.local import LocalOllamaProvider  # noqa: PLC0415

        return LocalOllamaProvider(
            chosen_model, settings.ollama_url, timeout=EMBED_TIMEOUT, batch_size=batch
        )
    from ...project_context.embeddings.remote import RemoteProvider  # noqa: PLC0415

    if not settings.embed_allow_remote:
        raise EmbeddingUnavailableError(
            "the remote embedding provider is disabled",
            hint="set KE_EMBED_ALLOW_REMOTE=1 (corpus text is then sent to KE_EMBED_REMOTE_URL)",
            details={"provider": "remote"},
        )
    if not settings.embed_remote_url.strip():
        raise EmbeddingUnavailableError(
            "the remote embedding provider has no endpoint",
            hint="set KE_EMBED_REMOTE_URL",
            details={"provider": "remote"},
        )
    return RemoteProvider.create(
        endpoint=settings.embed_remote_url, model=chosen_model, allow_remote=True
    )


__all__ = [
    "EMBEDDINGS_FILENAME",
    "EMBED_TIMEOUT",
    "build_provider",
    "default_db_path",
]
