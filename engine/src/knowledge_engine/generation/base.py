"""Core types for LLM generation routing: request/response + provider contract."""

from __future__ import annotations

import builtins
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)

#: Default seconds an availability probe result is reused (``KE_PROVIDER_PROBE_TTL``).
DEFAULT_PROBE_TTL = 30.0


@dataclass
class GenerationRequest:
    prompt: str
    model: str | None = None
    max_tokens: int = 1024
    temperature: float = 0.2
    metadata: dict[str, Any] | None = None


@dataclass
class GenerationResponse:
    text: str
    provider: str
    model: str
    usage: dict[str, Any] | None = None


class ProviderError(RuntimeError):
    """A provider could not produce a completion (network, HTTP or protocol failure).

    The HTTP layer maps it to ``502 Bad Gateway``. ``status_code`` is the
    upstream HTTP status when there was one. Messages never contain
    credentials.
    """

    def __init__(self, provider: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message
        self.status_code = status_code


class Provider(ABC):
    """Abstract provider. Implementations live in cloud_http.py / ollama_provider.py.

    ``generate`` raises :class:`ProviderError` for upstream failures.
    ``available`` must not raise; it may perform a (short) network probe,
    which :class:`ProviderRegistry` caches.
    """

    #: False when the provider was registered with its built-in default
    #: endpoint rather than operator configuration; an unreachable default is
    #: reported at INFO instead of WARNING.
    explicitly_configured: bool = True

    name: str = "abstract"

    @abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResponse: ...

    @abstractmethod
    def available(self) -> bool: ...


class ProviderRegistry:
    """In-memory provider registry. First available provider wins by default.

    ``available()`` results are cached per provider for ``probe_ttl`` seconds
    (``0`` disables the cache), so a request does not pay a network probe for
    every registered provider. The cache is thread-safe; a probe runs outside
    the lock so a slow provider never blocks lookups of the others.
    """

    def __init__(
        self,
        probe_ttl: float = DEFAULT_PROBE_TTL,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if probe_ttl < 0:
            raise ValueError("probe_ttl must be >= 0")
        self._providers: list[Provider] = []
        self._probe_ttl = float(probe_ttl)
        self._clock = clock
        self._lock = threading.Lock()
        self._probes: dict[str, tuple[bool, float]] = {}

    @property
    def probe_ttl(self) -> float:
        return self._probe_ttl

    def register(self, provider: Provider) -> None:
        with self._lock:
            if any(p.name == provider.name for p in self._providers):
                _logger.warning(
                    "provider %r registered twice; the first registration wins", provider.name
                )
            self._providers.append(provider)

    def list(self) -> list[str]:
        """Registered provider names, in selection order."""
        with self._lock:
            return [p.name for p in self._providers]

    def providers(self) -> builtins.list[Provider]:
        with self._lock:
            return list(self._providers)

    def get(self, name: str) -> Provider | None:
        with self._lock:
            return next((p for p in self._providers if p.name == name), None)

    def reorder(self, order: Iterable[str]) -> builtins.list[str]:
        """Move the named providers to the front, in ``order``; return unknown names.

        Unlisted providers keep their relative order behind the listed ones.
        """
        wanted = [name.strip().lower() for name in order if name.strip()]
        with self._lock:
            by_name = {p.name: p for p in self._providers}
            unknown = [name for name in wanted if name not in by_name]
            front: list[Provider] = []
            for name in wanted:
                provider = by_name.get(name)
                if provider is not None and provider not in front:
                    front.append(provider)
            self._providers = front + [p for p in self._providers if p not in front]
        return unknown

    # ── availability ─────────────────────────────────────────────

    def _cached(self, name: str) -> bool | None:
        if self._probe_ttl <= 0:
            return None
        with self._lock:
            hit = self._probes.get(name)
        if hit is None:
            return None
        ok, at = hit
        if self._clock() - at >= self._probe_ttl:
            return None
        return ok

    def probe(self, provider: Provider) -> bool:
        """Probe ``provider`` now (ignoring the cache) and remember the result."""
        try:
            ok = bool(provider.available())
        except Exception:
            _logger.exception("availability probe of provider %r raised", provider.name)
            ok = False
        with self._lock:
            self._probes[provider.name] = (ok, self._clock())
        return ok

    def is_available(self, provider: Provider) -> bool:
        """Cached availability of ``provider`` (probes when stale)."""
        cached = self._cached(provider.name)
        if cached is not None:
            return cached
        return self.probe(provider)

    def invalidate(self, name: str | None = None) -> None:
        """Forget cached probe results (all, or one provider's)."""
        with self._lock:
            if name is None:
                self._probes.clear()
            else:
                self._probes.pop(name, None)

    def probe_all(self, *, warn: bool = False) -> dict[str, bool]:
        """Probe every provider now; with ``warn``, log unreachable ones at WARNING."""
        results: dict[str, bool] = {}
        for provider in self.providers():
            ok = self.probe(provider)
            results[provider.name] = ok
            if warn and not ok:
                if getattr(provider, "explicitly_configured", True):
                    _logger.warning(
                        "generation provider %r is configured but unreachable; "
                        "requests fall through to the next available provider",
                        provider.name,
                    )
                else:
                    _logger.info(
                        "generation provider %r is not reachable at its default endpoint; "
                        "skipped until it answers",
                        provider.name,
                    )
        return results

    def select(self, preferred: str | None = None) -> Provider | None:
        """The preferred provider when it is available, else the first available one."""
        providers = self.providers()
        if preferred:
            for p in providers:
                if p.name == preferred and self.is_available(p):
                    return p
            _logger.debug("preferred provider %r unavailable; falling back", preferred)
        for p in providers:
            if self.is_available(p):
                return p
        return None
