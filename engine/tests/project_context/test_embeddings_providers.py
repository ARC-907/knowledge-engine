"""Offline tests for the project-context embedding providers.

These tests never touch the network. The Ollama and remote providers are
exercised only for their construction-time gates and pure parsing helpers; the
:class:`StubProvider` and vector helpers cover the deterministic math.
"""

from __future__ import annotations

import math
import struct
from types import SimpleNamespace

import pytest

from knowledge_engine.project_context.config import EmbeddingsCfg, ProjectContextConfig
from knowledge_engine.project_context.embeddings.providers import (
    VECTOR_FORMAT_VERSION,
    EmbeddingProvider,
    FakeProvider,
    StubProvider,
    VectorDimensionError,
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
    # Real ProjectContextConfig: embeddings disabled, provider "none".
    assert get_provider(ProjectContextConfig()) is None


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
    from knowledge_engine.project_context.embeddings.local import OllamaProvider

    cfg = ProjectContextConfig(embeddings=EmbeddingsCfg(enabled=True, provider="ollama"))
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
    # One format-version byte, then little-endian float32 components.
    assert len(blob) == 1 + len(original) * 4
    assert blob[0] == VECTOR_FORMAT_VERSION
    restored = unpack_vector(blob)
    assert len(restored) == len(original)
    for a, b in zip(original, restored, strict=True):
        assert math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)


def test_pack_unpack_empty() -> None:
    assert pack_vector([]) == bytes([VECTOR_FORMAT_VERSION])
    assert unpack_vector(pack_vector([])) == []
    assert unpack_vector(b"") == []


def test_unpack_reads_legacy_headerless_blobs() -> None:
    """Vectors stored before the version byte existed stay readable."""
    legacy = struct.pack("<3f", 0.5, -1.0, 2.0)
    assert unpack_vector(legacy) == [0.5, -1.0, 2.0]


def test_unpack_rejects_unknown_version() -> None:
    with pytest.raises(ValueError, match="version"):
        unpack_vector(bytes([99]) + struct.pack("<2f", 1.0, 2.0))


def test_pack_rejects_non_finite_and_out_of_range() -> None:
    with pytest.raises(ValueError):
        pack_vector([float("nan")])
    with pytest.raises(ValueError):
        pack_vector([float("inf")])
    with pytest.raises(ValueError):
        pack_vector([1e300])


def test_unpack_rejects_bad_length() -> None:
    with pytest.raises(ValueError):
        unpack_vector(b"\x00\x00\x00")


def test_stub_vector_round_trips() -> None:
    provider = StubProvider(dim=8)
    [vector] = provider.embed(["round trip"])
    restored = unpack_vector(pack_vector(vector))
    for a, b in zip(vector, restored, strict=True):
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


def test_cosine_mismatched_length_raises() -> None:
    """Vectors of different models cannot be compared: refuse instead of truncating."""
    with pytest.raises(VectorDimensionError, match=r"2 vs 1"):
        cosine([1.0, 2.0], [1.0])


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
    from knowledge_engine.project_context.embeddings.local import OllamaProvider

    provider = OllamaProvider(model="nomic-embed-text", url="http://localhost:11434")
    assert provider.name == "ollama:nomic-embed-text"
    assert provider.dim == 0


def test_ollama_embed_requires_model() -> None:
    from knowledge_engine.project_context.embeddings.local import OllamaProvider

    provider = OllamaProvider(model="", url="http://localhost:11434")
    with pytest.raises(RuntimeError):
        provider.embed(["text"])


def test_ollama_parse_vector_embeddings_shape() -> None:
    from knowledge_engine.project_context.embeddings.local import OllamaProvider

    body = '{"embeddings": [[0.1, 0.2, 0.3]]}'
    assert OllamaProvider._parse_vector(body) == pytest.approx([0.1, 0.2, 0.3])


def test_ollama_parse_vector_embedding_shape() -> None:
    from knowledge_engine.project_context.embeddings.local import OllamaProvider

    body = '{"embedding": [0.4, 0.5]}'
    assert OllamaProvider._parse_vector(body) == pytest.approx([0.4, 0.5])


def test_ollama_parse_vector_rejects_empty() -> None:
    from knowledge_engine.project_context.embeddings.local import OllamaProvider

    with pytest.raises(RuntimeError):
        OllamaProvider._parse_vector('{"embeddings": []}')


def test_remote_disabled_by_default() -> None:
    from knowledge_engine.project_context.embeddings.remote import RemoteProvider

    with pytest.raises(RuntimeError):
        RemoteProvider(_cfg(enabled=True, provider="remote", allow_remote_provider=False))


def test_remote_disabled_for_real_default_config() -> None:
    from knowledge_engine.project_context.embeddings.remote import RemoteProvider

    with pytest.raises(RuntimeError):
        RemoteProvider(ProjectContextConfig())


def test_remote_allowed_constructs() -> None:
    from knowledge_engine.project_context.embeddings.remote import RemoteProvider

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
        get_provider(_cfg(enabled=True, provider="remote", allow_remote_provider=False))


# --------------------------------------------------------------------------- #
# FakeProvider / model_id
# --------------------------------------------------------------------------- #


def test_fake_provider_is_deterministic_unit_length_and_word_sensitive() -> None:
    provider = FakeProvider(dim=32)
    a1, a2, b, c = provider.embed(
        ["the red fox runs", "the red fox runs", "a red fox sleeps", "quantum chromodynamics"]
    )
    assert a1 == a2
    assert len(a1) == provider.dim == 32
    assert math.isclose(math.sqrt(sum(x * x for x in a1)), 1.0, rel_tol=1e-9)
    # Shared words make texts closer than unrelated ones.
    assert cosine(a1, b) > cosine(a1, c)


def test_fake_provider_never_returns_a_zero_vector() -> None:
    [vector] = FakeProvider(dim=8).embed(["!!! ???"])
    assert any(vector)


def test_fake_provider_identity_and_validation() -> None:
    provider = FakeProvider(dim=16)
    assert provider.name == "fake"
    assert provider.model_id == "fake-hash-16"
    with pytest.raises(ValueError):
        FakeProvider(dim=0)


def test_get_provider_returns_fake_when_configured() -> None:
    provider = get_provider(_cfg(enabled=True, provider="fake", dim=12))
    assert isinstance(provider, FakeProvider)
    assert provider.dim == 12


def test_model_id_defaults_to_name_and_names_the_model() -> None:
    from knowledge_engine.project_context.embeddings.local import (
        LocalOllamaProvider,
        OllamaProvider,
    )
    from knowledge_engine.project_context.embeddings.remote import RemoteProvider

    assert StubProvider().model_id == "stub"
    assert OllamaProvider is LocalOllamaProvider
    assert LocalOllamaProvider(model="bge-m3").model_id == "bge-m3"
    # The project-context store keys rows by provider name: unchanged.
    assert LocalOllamaProvider(model="bge-m3").name == "ollama:bge-m3"
    remote = RemoteProvider.create(endpoint="http://e/", model="m", allow_remote=True)
    assert (remote.name, remote.model_id) == ("remote:m", "m")
    with pytest.raises(RuntimeError, match="disabled"):
        RemoteProvider.create(endpoint="http://e/", model="m", allow_remote=False)
