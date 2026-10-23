"""Generation providers: probe cache, ordering, the OpenAI-compatible cloud client, errors.

No test touches the network: the cloud client runs on ``httpx.MockTransport``
and Ollama's module-level ``httpx`` calls are monkeypatched.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from knowledge_engine.core.settings import Settings
from knowledge_engine.generation import (
    GenerationRequest,
    GenerationResponse,
    Provider,
    ProviderError,
    ProviderRegistry,
)
from knowledge_engine.generation.cloud_http import CloudHTTPProvider, EchoProvider
from knowledge_engine.generation.factory import build_provider_registry
from knowledge_engine.generation.ollama_provider import OllamaProvider

ENDPOINT = "https://llm.example/v1"
SECRET = "sk-test-secret"


class CountingProvider(Provider):
    def __init__(self, name: str, up: bool = True) -> None:
        self.name = name
        self.up = up
        self.probes = 0

    def available(self) -> bool:
        self.probes += 1
        return self.up

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        return GenerationResponse(text=request.prompt, provider=self.name, model="m")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ── ProviderRegistry ─────────────────────────────────────────────


def test_probe_results_are_cached_for_the_ttl() -> None:
    clock = FakeClock()
    down = CountingProvider("down", up=False)
    up = CountingProvider("up")
    reg = ProviderRegistry(probe_ttl=30, clock=clock)
    reg.register(down)
    reg.register(up)

    for _ in range(5):
        assert reg.select() is up
    assert (down.probes, up.probes) == (1, 1)

    clock.now += 29.9
    reg.select()
    assert (down.probes, up.probes) == (1, 1)

    clock.now += 0.2  # past the TTL: re-probe once
    reg.select()
    assert (down.probes, up.probes) == (2, 2)

    reg.invalidate("up")
    reg.select()
    assert (down.probes, up.probes) == (2, 3)


def test_zero_ttl_probes_every_time_and_preferred_falls_back() -> None:
    a = CountingProvider("a")
    b = CountingProvider("b")
    reg = ProviderRegistry(probe_ttl=0)
    reg.register(a)
    reg.register(b)
    assert reg.select(preferred="b") is b
    assert reg.select(preferred="b") is b
    assert b.probes == 2
    b.up = False
    assert reg.select(preferred="b") is a
    assert reg.select(preferred="nope") is a
    with pytest.raises(ValueError, match="probe_ttl"):
        ProviderRegistry(probe_ttl=-1)


def test_probe_that_raises_counts_as_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    class Broken(CountingProvider):
        def available(self) -> bool:
            raise RuntimeError("boom")

    reg = ProviderRegistry()
    reg.register(Broken("broken"))
    with caplog.at_level(logging.ERROR, logger="knowledge_engine.generation.base"):
        assert reg.select() is None
    assert "broken" in caplog.text


def test_probe_all_warns_about_unreachable_providers(caplog: pytest.LogCaptureFixture) -> None:
    reg = ProviderRegistry()
    reg.register(CountingProvider("gone", up=False))
    reg.register(CountingProvider("here"))
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.generation.base"):
        assert reg.probe_all(warn=True) == {"gone": False, "here": True}
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "'gone'" in warnings[0]


def test_reorder_moves_listed_providers_first() -> None:
    reg = ProviderRegistry()
    for name in ("cloud-http", "ollama", "echo"):
        reg.register(CountingProvider(name))
    assert reg.reorder(["echo", "missing", "ollama"]) == ["missing"]
    assert reg.list() == ["echo", "ollama", "cloud-http"]


# ── Factory (KE_PROVIDER_ORDER, cloud registration) ──────────────


def test_factory_default_order_and_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("building the registry must not touch the network")

    monkeypatch.setattr(httpx, "get", no_network)
    monkeypatch.setattr(httpx.Client, "get", no_network)
    reg = build_provider_registry(Settings())
    assert reg.list() == ["ollama", "echo"]
    reg = build_provider_registry(
        Settings(cloud_endpoint=ENDPOINT, cloud_api_key=SECRET, cloud_model="m")
    )
    assert reg.list() == ["cloud-http", "ollama", "echo"]
    assert reg.probe_ttl == 30.0


def test_factory_honours_provider_order_and_warns_on_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        cloud_endpoint=ENDPOINT,
        cloud_api_key=SECRET,
        cloud_model="m",
        provider_order="ollama, nosuch",
        provider_probe_ttl=5,
    )
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.generation.factory"):
        reg = build_provider_registry(settings)
    assert reg.list() == ["ollama", "cloud-http", "echo"]
    assert reg.probe_ttl == 5
    assert "nosuch" in caplog.text


def test_factory_warns_about_risky_cloud_configuration(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(cloud_endpoint="http://llm.example/v1", cloud_api_key=SECRET)
    with caplog.at_level(logging.WARNING, logger="knowledge_engine.generation.factory"):
        build_provider_registry(settings)
    assert "unencrypted" in caplog.text
    assert "KE_CLOUD_MODEL" in caplog.text
    assert SECRET not in caplog.text


# ── CloudHTTPProvider ────────────────────────────────────────────


def _cloud(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> CloudHTTPProvider:
    params: dict[str, Any] = {
        "endpoint": ENDPOINT,
        "api_key": SECRET,
        "model": "gpt-test",
        "timeout": 7.0,
    }
    params.update(kwargs)
    return CloudHTTPProvider(**params, transport=httpx.MockTransport(handler))


def _completion(content: Any, **extra: Any) -> httpx.Response:
    body = {
        "model": "gpt-test-0613",
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    body.update(extra)
    return httpx.Response(200, json=body)


def test_cloud_generate_posts_chat_completions() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _completion("hello there", usage={"total_tokens": 7})

    provider = _cloud(handler)
    res = provider.generate(GenerationRequest(prompt="hi", max_tokens=12, temperature=0.5))
    assert (res.text, res.provider, res.model) == ("hello there", "cloud-http", "gpt-test-0613")
    assert res.usage == {"total_tokens": 7}
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == f"{ENDPOINT}/chat/completions"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    payload = json.loads(request.content)
    assert payload == {
        "model": "gpt-test",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 12,
        "temperature": 0.5,
    }


def test_cloud_request_model_overrides_default_and_full_url_is_accepted() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _completion([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])

    provider = _cloud(handler, endpoint=f"{ENDPOINT}/chat/completions/")
    res = provider.generate(GenerationRequest(prompt="x", model="other"))
    assert res.text == "ab"
    assert str(seen[0].url) == f"{ENDPOINT}/chat/completions"
    assert json.loads(seen[0].content)["model"] == "other"


def test_cloud_null_content_is_empty_text() -> None:
    assert _cloud(lambda _r: _completion(None)).generate(GenerationRequest("x")).text == ""


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (httpx.Response(500, text="upstream exploded"), "HTTP 500"),
        (httpx.Response(401, json={"error": "bad key"}), "HTTP 401"),
        (httpx.Response(200, text="not json"), "invalid JSON"),
        (httpx.Response(200, json=[1, 2]), "non-object"),
        (httpx.Response(200, json={"choices": []}), "no choices"),
        (httpx.Response(200, json={"choices": [{"text": "legacy"}]}), "no message"),
    ],
)
def test_cloud_upstream_failures_raise_provider_error(response: httpx.Response, match: str) -> None:
    provider = _cloud(lambda _r: response)
    with pytest.raises(ProviderError, match=match) as exc:
        provider.generate(GenerationRequest(prompt="hi"))
    assert exc.value.provider == "cloud-http"
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize(
    ("error", "match"),
    [
        (httpx.ConnectError("refused"), "ConnectError"),
        (httpx.ReadTimeout("slow"), "timed out after 7s"),
    ],
)
def test_cloud_network_failures_raise_provider_error(error: Exception, match: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(ProviderError, match=match):
        _cloud(handler).generate(GenerationRequest(prompt="hi"))


def test_cloud_requires_configuration_and_a_model() -> None:
    with pytest.raises(ProviderError, match="not configured"):
        CloudHTTPProvider(endpoint="", api_key="", model="m", timeout=1).generate(
            GenerationRequest("x")
        )
    with pytest.raises(ProviderError, match="no model"):
        _cloud(lambda _r: _completion("x"), model="").generate(GenerationRequest("x"))


@pytest.mark.parametrize(
    ("status", "expected"), [(200, True), (404, True), (401, False), (403, False), (503, False)]
)
def test_cloud_availability_probe(status: int, expected: bool) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={})

    assert _cloud(handler).available() is expected
    assert str(seen[0].url) == f"{ENDPOINT}/models"
    assert seen[0].method == "GET"


def test_cloud_unconfigured_or_unreachable_is_unavailable() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert _cloud(refuse).available() is False
    calls: list[httpx.Request] = []
    unconfigured = _cloud(lambda r: calls.append(r) or httpx.Response(200), api_key="")
    assert unconfigured.available() is False
    assert calls == []
    assert _cloud(lambda _r: httpx.Response(200), endpoint="ftp://x").available() is False


def test_cloud_reads_settings_when_arguments_are_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KE_CLOUD_ENDPOINT", ENDPOINT + "/")
    monkeypatch.setenv("KE_CLOUD_API_KEY", SECRET)
    monkeypatch.setenv("KE_CLOUD_MODEL", "env-model")
    monkeypatch.setenv("KE_CLOUD_TIMEOUT", "12")
    provider = CloudHTTPProvider()
    assert provider.configured()
    assert (provider.base_url, provider.model, provider.timeout) == (ENDPOINT, "env-model", 12.0)
    assert provider.config_problems() == []


# ── Ollama / echo ────────────────────────────────────────────────


def test_ollama_errors_become_provider_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = OllamaProvider(settings=Settings())

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", refuse)
    with pytest.raises(ProviderError, match="ConnectError"):
        provider.generate(GenerationRequest("x"))
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: httpx.Response(500, text="oops"))
    with pytest.raises(ProviderError, match="HTTP 500") as exc:
        provider.generate(GenerationRequest("x"))
    assert exc.value.status_code == 500
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *_a, **_k: httpx.Response(200, json={"response": "ok", "eval_count": 3}),
    )
    res = provider.generate(GenerationRequest("x"))
    assert (res.text, res.provider, res.model) == ("ok", "ollama", "llama3.2")
    assert res.usage == {"eval_count": 3, "prompt_eval_count": None}


def test_ollama_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu:11434/")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen")
    provider = OllamaProvider()
    assert (provider.base_url, provider.model) == ("http://gpu:11434", "qwen")
    assert OllamaProvider("http://x", "y", timeout=1).base_url == "http://x"


def test_echo_provider_is_always_available() -> None:
    echo = EchoProvider()
    assert echo.available()
    assert echo.generate(GenerationRequest("ping")).text == "ping"
