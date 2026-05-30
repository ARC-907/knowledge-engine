"""Offline tests for the project-docs embedding providers.

These tests never touch the network. The Ollama and remote providers are
exercised only for their construction-time gates and pure parsing helpers; the
:class:`StubProvider` and vector helpers cover the deterministic math.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from knowledge_engine.project_docs.config import EmbeddingsCfg, ProjectDocsConfig
from knowledge_engine.project_docs.embeddings.providers import (
    EmbeddingProvider,
    StubProvider,
    cosine,
    get_provider,
    pack_vector,
    unpack_vector,
)


def _cfg(**embeddings: object) -> SimpleNamespace:
    """Build a minimal config-like object with an ``embeddings`` namespace."""
    return SimpleNamespace(embeddings=SimpleNamespace(**embeddings))


# --------------------------------------------------------------------------- #
# get_provider gating
# --------------------------------------------------------------------------- #


def test_get_provider_none_for_real_default_config() -> None:
    # Real ProjectDocsConfig: embeddings disabled, provider "none".
    assert get_provider(ProjectDocsConfig()) is None


def test_get_provider_none_when_disabled() -> None:
    assert get_provider(_cfg(enabled=False, provider="stub")) is None


def test_get_provider_none_when_provider_empty() -> None:
    assert get_provider(_cfg(enabled=True, provider="")) is None


def test_get_provider_none_when_provider_none_literal() -> None:
    assert get_provider(_cfg(enabled=True, provider="none")) is None


def test_get_provider_none_when_no_embeddings_attr() -> None:
    assert get_provider(SimpleNamespace()) is None


def test_get_provider_none_for_unknown_provider() -> None:
    assert get_provider(_cfg(enabled=True, provider="mystery")) is None


def test_get_provider_returns_stub_when_configured() -> None:
    provider = get_provider(_cfg(enabled=True, provider="stub", dim=8))
    assert isinstance(provider, StubProvider)
    assert isinstance(provider, EmbeddingProvider)
    assert provider.dim == 8


def test_get_provider_returns_ollama_for_real_config() -> None:
    from knowledge_engine.project_docs.embeddings.local import OllamaProvider

    cfg = ProjectDocsConfig(
        embeddings=EmbeddingsCfg(enabled=True, provider="ollama")
    )
    provider = get_provider(cfg)
    assert isinstance(provider, OllamaProvider)


# --------------------------------------------------------------------------- #
# StubProvider determinism + dimensionality
# --------------------------------------------------------------------------- #


def test_stub_embed_correct_dim() -> None:
    provider = StubProvider(dim=8)
    vectors = provider.embed(["alpha", "beta", "gamma"])
    assert len(vectors) == 3
    assert all(len(v) == 8 for v in vectors)


def test_stub_embed_deterministic() -> None:
    provider = StubProvider(dim=8)
    first = provider.embed(["repeatable text"])
    second = provider.embed(["repeatable text"])
    assert first == second


def test_stub_embed_distinct_inputs_differ() -> None:
    provider = StubProvider(dim=8)
    [vec_a] = provider.embed(["alpha"])
    [vec_b] = provider.embed(["beta"])
    assert vec_a != vec_b


def test_stub_custom_dim() -> None:
    provider = StubProvider(dim=16)
    [vector] = provider.embed(["x"])
    assert provider.dim == 16
    assert len(vector) == 16


def test_stub_rejects_nonpositive_dim() -> None:
    with pytest.raises(ValueError):
        StubProvider(dim=0)


def test_stub_name() -> None:
    assert StubProvider().name == "stub"


# --------------------------------------------------------------------------- #
# pack / unpack round-trip
# --------------------------------------------------------------------------- #


def test_pack_unpack_round_trip() -> None:
    original = [0.0, 1.0, -1.0, 0.5, -0.25]
    blob = pack_vector(original)
    assert isinstance(blob, bytes)
    assert len(blob) == len(original) * 4
    restored = unpack_vector(blob)
    assert len(restored) == len(original)
    for a, b in zip(original, restored):
        assert math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)


def test_pack_unpack_empty() -> None:
    assert pack_vector([]) == b""
    assert unpack_vector(b"") == []


def test_unpack_rejects_bad_length() -> None:
    with pytest.raises(ValueError):
        unpack_vector(b"\x00\x00\x00")


def test_stub_vector_round_trips() -> None:
    provider = StubProvider(dim=8)
    [vector] = provider.embed(["round trip"])
    restored = unpack_vector(pack_vector(vector))
    for a, b in zip(vector, restored):
        assert math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)


# --------------------------------------------------------------------------- #
# cosine similarity
# --------------------------------------------------------------------------- #


def test_cosine_identical_is_one() -> None:
    vector = [1.0, 2.0, 3.0, 4.0]
    assert math.isclose(cosine(vector, vector), 1.0, rel_tol=1e-6, abs_tol=1e-6)


def test_cosine_orthogonal_is_zero() -> None:
    assert math.isclose(cosine([1.0, 0.0], [0.0, 1.0]), 0.0, abs_tol=1e-9)


def test_cosine_opposite_is_minus_one() -> None:
    assert math.isclose(cosine([1.0, 1.0], [-1.0, -1.0]), -1.0, rel_tol=1e-6)


def test_cosine_zero_vector_is_zero() -> None:
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_mismatched_length_is_zero() -> None:
    assert cosine([1.0, 2.0], [1.0]) == 0.0


def test_cosine_empty_is_zero() -> None:
    assert cosine([], []) == 0.0


def test_cosine_stub_identical_text() -> None:
    provider = StubProvider(dim=8)
    [vec] = provider.embed(["same"])
    assert math.isclose(cosine(vec, vec), 1.0, rel_tol=1e-6, abs_tol=1e-6)


# --------------------------------------------------------------------------- #
# OllamaProvider / RemoteProvider: construction + gates, no network
# --------------------------------------------------------------------------- #


def test_ollama_no_network_at_construction() -> None:
    from knowledge_engine.project_docs.embeddings.local import OllamaProvider

    provider = OllamaProvider(model="nomic-embed-text", url="http://localhost:11434")
    assert provider.name == "ollama:nomic-embed-text"
    assert provider.dim == 0


def test_ollama_embed_requires_model() -> None:
    from knowledge_engine.project_docs.embeddings.local import OllamaProvider

    provider = OllamaProvider(model="", url="http://localhost:11434")
    with pytest.raises(RuntimeError):
        provider.embed(["text"])


def test_ollama_parse_vector_embeddings_shape() -> None:
    from knowledge_engine.project_docs.embeddings.local import OllamaProvider

    body = '{"embeddings": [[0.1, 0.2, 0.3]]}'
    assert OllamaProvider._parse_vector(body) == pytest.approx([0.1, 0.2, 0.3])


def test_ollama_parse_vector_embedding_shape() -> None:
    from knowledge_engine.project_docs.embeddings.local import OllamaProvider

    body = '{"embedding": [0.4, 0.5]}'
    assert OllamaProvider._parse_vector(body) == pytest.approx([0.4, 0.5])


def test_ollama_parse_vector_rejects_empty() -> None:
    from knowledge_engine.project_docs.embeddings.local import OllamaProvider

    with pytest.raises(RuntimeError):
        OllamaProvider._parse_vector('{"embeddings": []}')


def test_remote_disabled_by_default() -> None:
    from knowledge_engine.project_docs.embeddings.remote import RemoteProvider

    with pytest.raises(RuntimeError):
        RemoteProvider(
            _cfg(enabled=True, provider="remote", allow_remote_provider=False)
        )


def test_remote_disabled_for_real_default_config() -> None:
    from knowledge_engine.project_docs.embeddings.remote import RemoteProvider

    with pytest.raises(RuntimeError):
        RemoteProvider(ProjectDocsConfig())


def test_remote_allowed_constructs() -> None:
    from knowledge_engine.project_docs.embeddings.remote import RemoteProvider

    provider = RemoteProvider(
        _cfg(
            enabled=True,
            provider="remote",
            allow_remote_provider=True,
            url="http://example.invalid/embed",
            model="remote-model",
        )
    )
    assert provider.name == "remote:remote-model"


def test_get_provider_remote_blocked_when_not_allowed() -> None:
    with pytest.raises(RuntimeError):
        get_provider(
            _cfg(enabled=True, provider="remote", allow_remote_provider=False)
        )
