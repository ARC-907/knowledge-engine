"""Build the engine's :class:`ProviderRegistry` from :class:`Settings` (no I/O)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import ProviderRegistry
from .cloud_http import CloudHTTPProvider, EchoProvider
from .ollama_provider import OllamaProvider

if TYPE_CHECKING:
    from ..core.settings import Settings

_logger = logging.getLogger(__name__)

#: Selection order when ``KE_PROVIDER_ORDER`` is unset.
DEFAULT_ORDER: tuple[str, ...] = ("cloud-http", "ollama", "echo")


def build_provider_registry(settings: Settings) -> ProviderRegistry:
    """Register the configured providers in selection order.

    * ``cloud-http`` -- only when ``KE_CLOUD_ENDPOINT`` and ``KE_CLOUD_API_KEY``
      are both set (configuration problems are logged at WARNING);
    * ``ollama`` -- always; whether it is reachable is probed lazily and
      cached for ``KE_PROVIDER_PROBE_TTL`` seconds;
    * ``echo`` -- always, the last-resort fallback.

    ``KE_PROVIDER_ORDER`` moves the listed providers to the front.
    Constructing the registry performs no network I/O.
    """
    registry = ProviderRegistry(probe_ttl=settings.provider_probe_ttl)
    if settings.cloud_configured():
        cloud = CloudHTTPProvider(settings=settings)
        for problem in cloud.config_problems():
            _logger.warning("cloud-http provider: %s", problem)
        registry.register(cloud)
    ollama = OllamaProvider(settings=settings)
    # Only an explicitly configured Ollama endpoint is worth a WARNING when it
    # is unreachable; the default loopback endpoint is probed quietly so a
    # deployment without Ollama (the container image, for one) does not log a
    # misleading "configured but unreachable" line at every start.
    ollama.explicitly_configured = "ollama_base_url" in settings.model_fields_set
    registry.register(ollama)
    registry.register(EchoProvider())
    order = settings.provider_order_list()
    if order:
        unknown = registry.reorder(order)
        if unknown:
            _logger.warning(
                "KE_PROVIDER_ORDER names unknown or unconfigured providers %s; registered: %s",
                unknown,
                registry.list(),
            )
    _logger.info("generation providers (selection order): %s", registry.list())
    return registry


__all__ = ["DEFAULT_ORDER", "build_provider_registry"]
