"""Cloud generation provider (OpenAI-compatible chat completions) and the echo fallback.

:class:`CloudHTTPProvider` talks to any server that implements the OpenAI
``POST /chat/completions`` API (OpenAI, Azure-style gateways, vLLM, LiteLLM,
OpenRouter, a local llama.cpp server ...). Bring your own key; nothing is
shipped by default.

Environment variables read (through :class:`~knowledge_engine.core.settings.Settings`):

* ``KE_CLOUD_ENDPOINT`` -- base URL, e.g. ``https://api.openai.com/v1``. The
  provider POSTs to ``<endpoint>/chat/completions`` (an endpoint that already
  ends in ``/chat/completions`` is used as is).
* ``KE_CLOUD_API_KEY``  -- sent as ``Authorization: Bearer <key>``.
* ``KE_CLOUD_MODEL``    -- default model when the request names none.
* ``KE_CLOUD_TIMEOUT``  -- request timeout in seconds (default 60).

The provider is registered only when both the endpoint and the key are set.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from ..core.logsafe import log_safe
from .base import GenerationRequest, GenerationResponse, Provider, ProviderError

if TYPE_CHECKING:
    from ..core.settings import Settings

_logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_PATH = "/chat/completions"
#: Upper bound for an availability probe, whatever ``KE_CLOUD_TIMEOUT`` says.
PROBE_TIMEOUT = 5.0
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ERROR_SNIPPET = 200


class EchoProvider(Provider):
    """Default fallback. Echoes the prompt back. Always available. Useful for tests."""

    name = "echo"

    def available(self) -> bool:
        return True

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        return GenerationResponse(
            text=request.prompt,
            provider=self.name,
            model=request.model or "echo-1",
        )


class CloudHTTPProvider(Provider):
    """OpenAI-compatible chat-completions client. Disabled until configured.

    Arguments left as ``None`` are taken from ``settings`` (default: the
    current environment). ``transport`` lets tests inject an
    ``httpx.MockTransport``.
    """

    name = "cloud-http"

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        *,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if settings is None and None in (endpoint, api_key, model, timeout):
            from ..core.settings import Settings  # noqa: PLC0415 - optional env fallback

            settings = Settings()
        secret = settings.cloud_api_key if settings is not None else None
        self.endpoint = (
            endpoint if endpoint is not None else settings.cloud_endpoint if settings else ""
        ).strip()
        self.api_key = (
            api_key if api_key is not None else secret.get_secret_value() if secret else ""
        ).strip()
        self.model = (
            model if model is not None else settings.cloud_model if settings else ""
        ).strip()
        self.timeout = float(
            timeout if timeout is not None else settings.cloud_timeout if settings else 60.0
        )
        self._transport = transport

    # ── configuration ────────────────────────────────────────────

    def configured(self) -> bool:
        """Endpoint and key are both set (the registration condition)."""
        return bool(self.endpoint and self.api_key)

    def config_problems(self) -> list[str]:
        """Human-readable issues with the configuration (empty when fine)."""
        problems: list[str] = []
        parts = urlsplit(self.endpoint)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            problems.append("KE_CLOUD_ENDPOINT must be an http(s) URL")
        elif parts.scheme == "http" and (parts.hostname or "") not in _LOOPBACK_HOSTS:
            problems.append(
                "KE_CLOUD_ENDPOINT uses plain http to a non-loopback host; "
                "the API key is sent unencrypted"
            )
        if not self.model:
            problems.append("KE_CLOUD_MODEL is unset; requests must name a model")
        return problems

    @property
    def base_url(self) -> str:
        base = self.endpoint.rstrip("/")
        if base.endswith(CHAT_COMPLETIONS_PATH):
            base = base[: -len(CHAT_COMPLETIONS_PATH)]
        return base

    @property
    def completions_url(self) -> str:
        return self.base_url + CHAT_COMPLETIONS_PATH

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(transport=self._transport, timeout=timeout)

    # ── Provider API ─────────────────────────────────────────────

    def available(self) -> bool:
        """Configured and the server answers ``GET <base>/models``.

        Network errors, timeouts, 5xx and rejected credentials (401/403)
        count as unavailable; any other answer (including 404 from servers
        without a models route) means the server is reachable.
        """
        if not self.configured():
            return False
        scheme = urlsplit(self.endpoint).scheme
        if scheme not in ("http", "https"):
            return False
        try:
            with self._client(min(self.timeout, PROBE_TIMEOUT)) as client:
                response = client.get(self.base_url + "/models", headers=self._headers())
        except (httpx.HTTPError, OSError) as exc:
            _logger.debug("cloud-http probe failed: %s", type(exc).__name__)
            return False
        if response.status_code in (401, 403):
            _logger.warning(
                "cloud-http: %s rejected the configured KE_CLOUD_API_KEY (HTTP %d)",
                log_safe(self.base_url),
                response.status_code,
            )
            return False
        return response.status_code < 500

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        if not self.configured():
            raise ProviderError(self.name, "not configured (KE_CLOUD_ENDPOINT / KE_CLOUD_API_KEY)")
        model = request.model or self.model
        if not model:
            raise ProviderError(self.name, "no model: set KE_CLOUD_MODEL or pass 'model'")
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": request.prompt}],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        try:
            with self._client(self.timeout) as client:
                response = client.post(self.completions_url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, f"timed out after {self.timeout:g}s") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise ProviderError(self.name, f"request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            snippet = log_safe(response.text[:_ERROR_SNIPPET])
            _logger.warning(
                "cloud-http: HTTP %d from %s: %s",
                response.status_code,
                log_safe(self.completions_url),
                snippet,
            )
            raise ProviderError(
                self.name,
                f"upstream returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return GenerationResponse(
            text=_completion_text(self.name, response),
            provider=self.name,
            model=_response_model(response) or model,
            usage=_response_usage(response),
        )


def _json_body(provider: str, response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError(provider, "upstream returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ProviderError(provider, "upstream returned a non-object JSON body")
    return data


def _completion_text(provider: str, response: httpx.Response) -> str:
    """The first choice's message content (string or a list of text parts)."""
    data = _json_body(provider, response)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError(provider, "upstream response has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ProviderError(provider, "upstream choice has no message")
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type", "text") == "text"
        )
    raise ProviderError(provider, "upstream message content has an unexpected type")


def _response_model(response: httpx.Response) -> str | None:
    try:
        model = response.json().get("model")
    except (ValueError, AttributeError):
        return None
    return model if isinstance(model, str) and model else None


def _response_usage(response: httpx.Response) -> dict[str, Any] | None:
    try:
        usage = response.json().get("usage")
    except (ValueError, AttributeError):
        return None
    return usage if isinstance(usage, dict) else None
