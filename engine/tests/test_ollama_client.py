"""OllamaClient and its consumers, offline (``httpx.MockTransport`` / monkeypatched httpx)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from knowledge_engine.core.ollama import (
    DEFAULT_BASE_URL,
    OllamaClient,
    OllamaError,
    normalize_base_url,
    parse_embeddings,
)

Handler = Callable[[httpx.Request], httpx.Response]
_dumps = json.dumps


def _client(handler: Handler, **kwargs: Any) -> tuple[OllamaClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = OllamaClient(
        kwargs.pop("base_url", "http://ollama.test:11434/"),
        kwargs.pop("timeout", 5.0),
        transport=httpx.MockTransport(record),
        **kwargs,
    )
    return client, seen


def _embed_reply(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    vectors = [[float(len(text)), 1.0] for text in body["input"]]
    return httpx.Response(200, json={"model": body["model"], "embeddings": vectors})


# ── construction ─────────────────────────────────────────────────────


def test_normalize_base_url() -> None:
    assert normalize_base_url(" http://h:1/ ") == "http://h:1"
    assert normalize_base_url("") == DEFAULT_BASE_URL
    assert normalize_base_url(None) == DEFAULT_BASE_URL


def test_construction_does_no_io_and_validates_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no I/O at construction")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    client = OllamaClient()
    assert client.base_url == DEFAULT_BASE_URL
    assert "OllamaClient(base_url=" in repr(client)
    with pytest.raises(ValueError, match="positive"):
        OllamaClient(timeout=0)


# ── available ────────────────────────────────────────────────────────


def test_available_probes_tags() -> None:
    client, seen = _client(lambda _r: httpx.Response(200, json={"models": []}))
    assert client.available() is True
    assert seen[0].method == "GET"
    assert str(seen[0].url) == "http://ollama.test:11434/api/tags"


def test_available_false_on_error_status_and_transport_failure() -> None:
    assert _client(lambda _r: httpx.Response(503))[0].available() is False

    def refuse(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert _client(refuse)[0].available() is False


# ── generate ─────────────────────────────────────────────────────────


def test_generate_payload_and_result() -> None:
    client, seen = _client(lambda _r: httpx.Response(200, json={"response": "hi", "eval_count": 2}))
    data = client.generate("m", "p", system="s", options={"temperature": 0.1})
    assert data == {"response": "hi", "eval_count": 2}
    sent = json.loads(seen[0].content)
    assert sent == {
        "model": "m",
        "prompt": "p",
        "stream": False,
        "system": "s",
        "options": {"temperature": 0.1},
    }
    assert str(seen[0].url).endswith("/api/generate")


@pytest.mark.parametrize(
    ("response", "match", "status"),
    [
        (httpx.Response(500, text="boom"), "HTTP 500", 500),
        (httpx.Response(200, text="not json"), "invalid JSON", None),
        (httpx.Response(200, json=[1]), "non-object", None),
    ],
)
def test_generate_failures_raise_ollama_error(
    response: httpx.Response, match: str, status: int | None
) -> None:
    client, _ = _client(lambda _r: response)
    with pytest.raises(OllamaError, match=match) as exc:
        client.generate("m", "p")
    assert exc.value.status_code == status


def test_generate_timeout_and_connect_error() -> None:
    def slow(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(OllamaError, match="timed out after 5s") as exc:
        _client(slow)[0].generate("m", "p")
    assert exc.value.timed_out is True

    def refuse(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(OllamaError, match="request failed: ConnectError"):
        _client(refuse)[0].generate("m", "p")


# ── embed ────────────────────────────────────────────────────────────


def test_embed_batches_in_order() -> None:
    client, seen = _client(_embed_reply)
    texts = [f"t{'x' * i}" for i in range(5)]
    vectors = client.embed(texts, "bge", batch_size=2)
    assert [v[0] for v in vectors] == [float(len(t)) for t in texts]
    assert [len(json.loads(r.content)["input"]) for r in seen] == [2, 2, 1]
    assert all(json.loads(r.content)["model"] == "bge" for r in seen)
    assert all(str(r.url).endswith("/api/embed") for r in seen)


def test_embed_empty_input_makes_no_request() -> None:
    client, seen = _client(_embed_reply)
    assert client.embed([], "bge") == []
    assert seen == []


def test_embed_validation() -> None:
    client, _ = _client(_embed_reply)
    with pytest.raises(OllamaError, match="model name"):
        client.embed(["a"], " ")
    with pytest.raises(ValueError, match="batch_size"):
        client.embed(["a"], "m", batch_size=0)


def test_embed_rejects_count_mismatch() -> None:
    client, _ = _client(lambda _r: httpx.Response(200, json={"embeddings": [[1.0]]}))
    with pytest.raises(OllamaError, match="1 embeddings for 2 input"):
        client.embed(["a", "b"], "m")


@pytest.mark.parametrize(
    ("data", "expected", "match"),
    [
        ([], 1, "non-object"),
        ({"embeddings": []}, 1, "no embedding vector"),
        ({"embeddings": [[]]}, 1, "empty or malformed"),
        ({"embeddings": [["x"]]}, 1, "non-numeric"),
        ({"embedding": [1.0]}, 2, "no embedding vector"),
    ],
)
def test_parse_embeddings_rejects(data: Any, expected: int, match: str) -> None:
    with pytest.raises(OllamaError, match=match):
        parse_embeddings(data, expected)


def test_parse_embeddings_accepts_legacy_single() -> None:
    assert parse_embeddings({"embedding": [1, 2]}, 1) == [[1.0, 2.0]]


# ── consumers ────────────────────────────────────────────────────────


def test_project_context_provider_uses_batched_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.project_context.embeddings.local import OllamaProvider

    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: Any, timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={"embeddings": [[0.5, 0.5, 0.5] for _ in json["input"]]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = OllamaProvider(model="nomic", url="http://gpu:11434/")
    vectors = provider.embed(["a", "b", "c"])
    assert len(vectors) == 3
    assert provider.dim == 3
    assert calls == [
        {
            "url": "http://gpu:11434/api/embed",
            "json": {"model": "nomic", "input": ["a", "b", "c"]},
            "timeout": 30.0,
        }
    ]
    assert provider.embed([]) == []


def test_project_context_provider_wraps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.project_context.embeddings.local import OllamaProvider

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", refuse)
    with pytest.raises(RuntimeError, match="Ollama embedding request failed"):
        OllamaProvider(model="m").embed(["a"])
    with pytest.raises(RuntimeError, match="requires a model"):
        OllamaProvider(model="").embed(["a"])


def test_corpus_embeddings_use_ke_ollama_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.retrieval.embeddings import build

    seen: list[str] = []

    def fake_post(url: str, *, json: Any, timeout: float) -> httpx.Response:
        seen.append(url)
        assert timeout == build.EMBED_TIMEOUT
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0] for _ in json["input"]]})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(build, "OLLAMA_URL", "http://embed-host:1234")
    assert build.get_embedding("hello") == [1.0, 0.0]
    assert seen == ["http://embed-host:1234/api/embed"]


def test_corpus_embed_chunks_falls_back_per_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.retrieval.embeddings import build

    def fake_post(url: str, *, json: Any, timeout: float) -> httpx.Response:
        texts = json["input"]
        if len(texts) > 1 or texts[0] == "bad":
            return httpx.Response(500, text="nope")
        return httpx.Response(200, json={"embeddings": [[1.0]]})

    monkeypatch.setattr(httpx, "post", fake_post)
    out = build._embed_chunks("f.md", [{"text": "good"}, {"text": "bad"}])
    assert out == [[1.0], None]


def test_task_classifier_uses_ollama_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowledge_engine.runtime.pipeline import task_classifier

    seen: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: Any, timeout: float) -> httpx.Response:
        seen.append({"url": url, "json": json, "timeout": timeout})
        reply = {"domain": "code", "complexity": "simple", "confidence": 0.9, "routing_note": "x"}
        return httpx.Response(200, json={"response": _dumps(reply)})

    monkeypatch.setattr(httpx, "post", fake_post)
    result = task_classifier.classify_task(
        "write a test", endpoint="http://cls:11434", model="tiny"
    )
    assert result["domain"] == "code"
    assert seen[0]["url"] == "http://cls:11434/api/generate"
    assert seen[0]["json"]["system"] == task_classifier.CLASSIFIER_PROMPT
    assert seen[0]["json"]["model"] == "tiny"
    assert seen[0]["timeout"] == 15.0

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", refuse)
    fallback = task_classifier.classify_task("x", endpoint="http://cls:11434/api/generate")
    assert fallback == task_classifier._default_classification()
