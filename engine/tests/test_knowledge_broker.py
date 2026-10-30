"""The retrieval broker (V2 P2): scope, filters before limit, fusion, failures, open.

Offline and deterministic: a synthetic corpus, the FTS index, a fake-provider
embedding index, synthetic project-context stores and fake adapters for the
failure paths. HTTP/MCP/CLI parity lives in ``test_knowledge_broker_interfaces.py``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from knowledge_engine.core.registry import RegistryEntry
from knowledge_engine.knowledge import backfill
from knowledge_engine.knowledge.adapter import AdapterCapabilities, AdapterSearchResult
from knowledge_engine.knowledge.backfill import CORPUS_SOURCE_ID, corpus_record_id
from knowledge_engine.knowledge.broker import KnowledgeBroker
from knowledge_engine.knowledge.contracts import (
    EffectiveScope,
    OpenResult,
    QueryContext,
    RankingInfo,
    RecordScope,
    SearchHit,
    SourceState,
    SourceStatus,
    TextLocator,
    TrustedPrincipal,
    VerificationState,
    derive_revision_id,
    sha256_digest,
)
from knowledge_engine.knowledge.errors import (
    DeniedError,
    InvalidInputError,
    InvalidScopeError,
    UnavailableError,
    UnknownScopeError,
)
from knowledge_engine.knowledge.sources import CorpusBackend, build_adapter_factory
from knowledge_engine.knowledge.sources.project_context import (
    link_project,
    source_id_for,
    unlink_project,
)
from knowledge_engine.knowledge.store import KnowledgeStore, ScopeSnapshot, SnapshotSource

WS = "default"
STRONG = "# Fox {n}\n\n" + "fox " * 30 + "\n"
WEAK = "# Weak fox {n}\n\nOne fox among " + "many other words here " * 30 + "\n"
TWIN = "# Twin\n\n" + "identical secret harbour notes about tides and moorings. " * 6 + "\n"


# ── fixtures ─────────────────────────────────────────────────────────


@dataclass
class World:
    root: Path
    corpus: Path
    store: KnowledgeStore
    backend: CorpusBackend
    extra: dict[str, Callable[[SnapshotSource, ScopeSnapshot], Any]] = field(default_factory=dict)
    broker: KnowledgeBroker = field(init=False)

    def ctx(self, **values: Any) -> QueryContext:
        return QueryContext(workspace_id=WS, **values)

    def rebuild_broker(self, backend: CorpusBackend | None = None) -> KnowledgeBroker:
        if hasattr(self, "broker"):
            self.broker.close()
        base = build_adapter_factory(backend or self.backend)

        def factory(source: SnapshotSource, snapshot: ScopeSnapshot) -> Any:
            if source.source_id in self.extra:
                return self.extra[source.source_id](source, snapshot)
            return base(source, snapshot)

        self.broker = KnowledgeBroker(self.store, factory)
        return self.broker


def _write(corpus: Path, rel: str, text: str) -> None:
    (corpus / rel).parent.mkdir(parents=True, exist_ok=True)
    (corpus / rel).write_text(text, encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    corpus = tmp_path / "corpus"
    for n in range(1, 11):
        _write(corpus, f"libs/a/strong{n:02d}.md", STRONG.format(n=n))
    for n in range(1, 6):
        _write(corpus, f"libs/a/weak{n:02d}.md", WEAK.format(n=n))
    _write(corpus, "libs/b/twin1.md", TWIN)
    _write(corpus, "libs/b/twin2.md", TWIN)
    _write(corpus, "libs/b/owl.md", "# Owls\n\nOwls are nocturnal birds.\n")
    data = tmp_path / "data"
    monkeypatch.setenv("KE_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("KE_REGISTRY_PATH", str(corpus / "registry.json"))
    monkeypatch.setenv("KE_DATA_DIR", str(data))
    monkeypatch.setenv("KE_PIPELINE_DB", str(data / "pipeline.db"))
    monkeypatch.setenv("KE_PIPELINE_ROOT", str(tmp_path / "pipeline"))
    monkeypatch.setenv("KE_CONFIG_PATH", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("KE_EMBED_PROVIDER", "fake")
    monkeypatch.chdir(tmp_path)

    from knowledge_engine.core.config import Config
    from knowledge_engine.core.registry import Registry
    from knowledge_engine.retrieval.embeddings.index import CorpusEmbeddingIndex
    from knowledge_engine.retrieval.fts5 import Indexer

    config = Config.from_env()
    config.ensure_dirs()
    registry = Registry(config.registry_path)
    registry.upsert(RegistryEntry(id="lib-a", kind="library", name="A", path="libs/a"))
    registry.upsert(RegistryEntry(id="lib-b", kind="library", name="B", path="libs/b"))
    indexer = Indexer(config, registry)
    indexer.rebuild()
    embeddings = CorpusEmbeddingIndex.from_settings()
    embeddings.build()
    backfill.apply(
        corpus_root=corpus,
        registry_path=corpus / "registry.json",
        db_path=data / "knowledge.db",
        workspace_id=WS,
    )
    store = KnowledgeStore.open(data / "knowledge.db")
    backend = CorpusBackend(
        indexer=indexer, registry=registry, corpus_root=corpus, embeddings=embeddings
    )
    w = World(tmp_path, corpus, store, backend)
    w.rebuild_broker()
    try:
        yield w
    finally:
        w.broker.close()
        store.close()
        indexer.close()
        registry.close()


def rid(path: str) -> str:
    return corpus_record_id(path)


def paths_of(envelope: Any) -> list[str]:
    return [hit.record_id for hit in envelope.hits]


# ── basic envelope ───────────────────────────────────────────────────


def test_fts_search_envelope_carries_identity_scope_and_status(world: World) -> None:
    env = world.broker.search(world.ctx(limit=3), "owls")
    assert [h.record_id for h in env.hits] == [rid("libs/b/owl.md")]
    hit = env.hits[0]
    data = (world.corpus / "libs/b/owl.md").read_bytes()
    assert hit.revision_id == derive_revision_id(
        CORPUS_SOURCE_ID, hit.record_id, sha256_digest(data)
    )
    assert hit.scope == RecordScope(workspace_id=WS)
    assert hit.provenance_id == "entry:lib-b"
    assert hit.ranking.native_rank == 1 and hit.ranking.score_kind == "bm25"
    assert hit.ranking.fused_rank == 1 and hit.ranking.fused_score is not None
    assert env.effective_scope.source_ids == (CORPUS_SOURCE_ID,)
    assert [s.state for s in env.source_status] == [SourceState.OK]
    assert env.source_status[0].hit_count == 1 and not env.partial


def test_no_match_is_a_healthy_empty_success(world: World) -> None:
    env = world.broker.search(world.ctx(), "zzzunmatchedterm")
    assert env.hits == () and not env.partial
    assert [s.state for s in env.source_status] == [SourceState.OK]


def test_explicit_empty_selection_searches_nothing(world: World) -> None:
    env = world.broker.search(world.ctx(source_ids=[]), "fox")
    assert env.hits == () and env.source_status == () and env.effective_scope.empty


@pytest.mark.parametrize("query", ["", "   ", "x" * 1001])
def test_invalid_query_is_rejected(world: World, query: str) -> None:
    with pytest.raises(InvalidInputError):
        world.broker.search(world.ctx(), query)


def test_invalid_fts_syntax_falls_back_to_a_phrase_with_a_warning(world: World) -> None:
    env = world.broker.search(world.ctx(), 'owls"')
    assert [h.record_id for h in env.hits] == [rid("libs/b/owl.md")]
    assert any("phrase" in w for w in env.warnings)


# ── scope ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ({"workspace_id": "nope"}, UnknownScopeError),
        ({"source_ids": ["corpus:nope"]}, UnknownScopeError),
        ({"collection_ids": ["nope"]}, UnknownScopeError),
        ({"project_id": "nope"}, UnknownScopeError),
        ({"profile_id": "nope"}, UnknownScopeError),
    ],
)
def test_unknown_scope_fails_the_request(world: World, values: dict[str, Any], error: type) -> None:
    context = QueryContext(**{"workspace_id": WS, **values})
    with pytest.raises(error):
        world.broker.search(context, "fox")
    with pytest.raises(error):
        world.broker.open(context, CORPUS_SOURCE_ID, rid("libs/b/owl.md"))


def test_foreign_project_collection_is_invalid_scope(world: World) -> None:
    world.store.create_project(WS, "P", project_id="p1")
    world.store.create_project(WS, "Q", project_id="q1")
    world.store.create_collection(WS, "Q only", collection_id="qc", project_id="q1")
    with pytest.raises(InvalidScopeError):
        world.broker.search(world.ctx(project_id="p1", collection_ids=["qc"]), "fox")


def test_principal_outside_the_workspace_is_denied(world: World) -> None:
    context = world.ctx().model_copy(
        update={"principal": TrustedPrincipal(principal_id="u", allowed_workspaces=("other",))}
    )
    with pytest.raises(DeniedError):
        world.broker.search(context, "fox")


def test_two_projects_with_one_name_and_one_text_never_leak(world: World) -> None:
    world.store.create_project(WS, "Thesis", project_id="alpha")
    world.store.create_project(WS, "Thesis", project_id="beta")
    world.store.classify(CORPUS_SOURCE_ID, rid("libs/b/twin1.md"), project_id="alpha")
    world.store.classify(CORPUS_SOURCE_ID, rid("libs/b/twin2.md"), project_id="beta")
    query = "identical secret harbour"
    alpha = world.broker.search(world.ctx(project_id="alpha"), query)
    beta = world.broker.search(world.ctx(project_id="beta"), query)
    assert paths_of(alpha) == [rid("libs/b/twin1.md")]
    assert paths_of(beta) == [rid("libs/b/twin2.md")]
    assert all(h.scope.project_id == "alpha" for h in alpha.hits)
    for mode in ("vector", "hybrid"):
        assert paths_of(world.broker.search(world.ctx(project_id="alpha", mode=mode), query)) == [
            rid("libs/b/twin1.md")
        ]
    both = world.broker.search(world.ctx(), query)
    assert sorted(paths_of(both)) == sorted([rid("libs/b/twin1.md"), rid("libs/b/twin2.md")])
    # A project query excludes workspace-wide records unless asked to include them.
    assert rid("libs/b/owl.md") not in paths_of(
        world.broker.search(world.ctx(project_id="alpha"), "owls")
    )
    included = world.broker.search(world.ctx(project_id="alpha", include_workspace=True), "owls")
    assert paths_of(included) == [rid("libs/b/owl.md")]
    # Open honours the same boundary: beta's record is unknown inside alpha.
    with pytest.raises(UnknownScopeError):
        world.broker.open(world.ctx(project_id="alpha"), CORPUS_SOURCE_ID, rid("libs/b/twin2.md"))


@pytest.mark.parametrize("mode", ["fts", "vector", "hybrid"])
def test_filters_apply_before_the_limit(world: World, mode: str) -> None:
    weak = [rid(f"libs/a/weak{n:02d}.md") for n in range(1, 6)]
    for record in weak:
        world.store.classify(CORPUS_SOURCE_ID, record, add_tags=["keep"])
    unfiltered = world.broker.search(world.ctx(limit=5, mode=mode), "fox")
    assert len(unfiltered.hits) == 5
    if mode == "fts":  # the strong files outrank every tagged file
        assert not set(paths_of(unfiltered)) & set(weak)
    env = world.broker.search(world.ctx(limit=5, mode=mode, tags=["keep"]), "fox")
    assert sorted(paths_of(env)) == sorted(weak)
    typed = world.broker.search(world.ctx(limit=3, mode=mode, research_types=["paper"]), "fox")
    assert typed.hits == () and not typed.partial


def test_multi_collection_record_is_returned_once_with_merged_membership(world: World) -> None:
    owl = rid("libs/b/owl.md")
    world.store.create_collection(WS, "Birds", collection_id="birds")
    world.store.create_collection(WS, "Night", collection_id="night")
    world.store.add_members("birds", [(CORPUS_SOURCE_ID, owl)])
    world.store.add_members("night", [(CORPUS_SOURCE_ID, owl)])
    env = world.broker.search(world.ctx(collection_ids=["birds", "night"]), "owls")
    assert paths_of(env) == [owl]
    assert env.hits[0].collection_ids == ("birds", "night")


# ── fusion and determinism ───────────────────────────────────────────


class FakeAdapter:
    """A configurable adapter for failure paths and fusion checks."""

    def __init__(
        self,
        source_id: str,
        hits: Callable[[EffectiveScope], list[SearchHit]] | None = None,
        *,
        error: Exception | None = None,
        gate: threading.Event | None = None,
        modes: tuple[str, ...] = ("fts", "vector", "hybrid"),
    ) -> None:
        self._source_id = source_id
        self._hits = hits or (lambda scope: [])
        self._error = error
        self._gate = gate
        self._modes = modes

    @property
    def source_id(self) -> str:
        return self._source_id

    def describe_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            source_id=self._source_id,
            source_kind="other",
            modes=self._modes,  # type: ignore[arg-type]
        )

    def health(self) -> SourceStatus:
        return SourceStatus(source_id=self._source_id, state=SourceState.OK)

    def search(
        self, context: QueryContext, scope: EffectiveScope, query: str
    ) -> AdapterSearchResult:
        if self._gate is not None:
            self._gate.wait(10)
        if self._error is not None:
            raise self._error
        hits = self._hits(scope)
        return AdapterSearchResult(
            source_id=self._source_id,
            hits=tuple(hits),
            status=SourceStatus(
                source_id=self._source_id, state=SourceState.OK, hit_count=len(hits)
            ),
        )

    def open(self, *args: Any, **kwargs: Any) -> OpenResult:
        raise UnavailableError("no open")


def fake_hit(source_id: str, record_id: str, rank: int, **extra: Any) -> SearchHit:
    values: dict[str, Any] = {
        "source_id": source_id,
        "record_id": record_id,
        "scope": RecordScope(workspace_id=WS),
        "title": record_id,
        "ranking": RankingInfo(native_rank=rank, native_score=float(rank), score_kind="other"),
    }
    values.update(extra)
    return SearchHit(**values)


def add_fake(world: World, source_id: str, adapter: Callable[[], FakeAdapter]) -> None:
    world.store.register_source(WS, source_id, kind="other", display_name=source_id)
    world.extra[source_id] = lambda source, snapshot: adapter()
    world.rebuild_broker()


def test_fusion_is_deterministic_and_ties_break_on_identity(world: World) -> None:
    add_fake(
        world,
        "fake:zz",
        lambda: FakeAdapter(
            "fake:zz", lambda s: [fake_hit("fake:zz", "r1", 1), fake_hit("fake:zz", "r2", 2)]
        ),
    )
    add_fake(
        world, "fake:aa", lambda: FakeAdapter("fake:aa", lambda s: [fake_hit("fake:aa", "r9", 1)])
    )
    context = world.ctx(source_ids=["fake:zz", "fake:aa"])
    runs = [world.broker.search(context, "q") for _ in range(3)]
    keys = [[(h.source_id, h.record_id, h.ranking.fused_rank) for h in env.hits] for env in runs]
    assert keys[0] == keys[1] == keys[2]
    # Equal RRF scores at rank 1: the identity ("fake:aa" < "fake:zz") breaks the tie.
    assert [(s, r) for s, r, _ in keys[0]] == [
        ("fake:aa", "r9"),
        ("fake:zz", "r1"),
        ("fake:zz", "r2"),
    ]
    real = [world.broker.search(world.ctx(mode="hybrid"), "fox").model_dump() for _ in range(2)]
    for envelope in real:
        for status in envelope["source_status"]:
            status["elapsed_ms"] = None
    assert real[0] == real[1]


def test_duplicate_hits_of_one_record_are_merged(world: World) -> None:
    add_fake(
        world,
        "fake:dup",
        lambda: FakeAdapter(
            "fake:dup",
            lambda s: [
                fake_hit("fake:dup", "same", 1, collection_ids=()),
                fake_hit("fake:dup", "same", 2),
                fake_hit("fake:dup", "other", 3),
            ],
        ),
    )
    env = world.broker.search(world.ctx(source_ids=["fake:dup"]), "q")
    assert [h.record_id for h in env.hits] == ["same", "other"]
    assert any("merged" in w for w in env.warnings)


def test_hits_outside_the_scope_are_dropped(world: World) -> None:
    world.store.create_project(WS, "P", project_id="p1")
    add_fake(
        world,
        "fake:rogue",
        lambda: FakeAdapter(
            "fake:rogue",
            lambda s: [
                fake_hit(
                    "fake:rogue", "mine", 1, scope=RecordScope(workspace_id=WS, project_id="p1")
                ),
                fake_hit("fake:rogue", "leak", 2, scope=RecordScope(workspace_id="other")),
                fake_hit("fake:rogue", "wide", 3),
            ],
        ),
    )
    env = world.broker.search(world.ctx(project_id="p1", source_ids=["fake:rogue"]), "q")
    assert [h.record_id for h in env.hits] == ["mine"]
    assert any("outside the effective scope" in w for w in env.warnings)


def test_byte_budget_cuts_hits(world: World) -> None:
    full = world.broker.search(world.ctx(limit=10), "fox")
    budget = sum(len(h.title.encode()) + len(h.excerpt.encode()) for h in full.hits[:2])
    env = world.broker.search(world.ctx(limit=10, byte_budget=budget), "fox")
    assert len(env.hits) == 2 and any("byte budget" in w for w in env.warnings)


# ── failures ─────────────────────────────────────────────────────────


def test_unavailable_optional_source_gives_partial(world: World) -> None:
    add_fake(world, "fake:down", lambda: FakeAdapter("fake:down", error=UnavailableError("down")))
    env = world.broker.search(world.ctx(), "owls")
    assert env.partial and paths_of(env) == [rid("libs/b/owl.md")]
    states = {s.source_id: s for s in env.source_status}
    assert states["fake:down"].state == SourceState.UNAVAILABLE
    assert states["fake:down"].error is not None and states["fake:down"].error.code == "unavailable"
    assert states[CORPUS_SOURCE_ID].state == SourceState.OK


def test_source_without_adapter_is_reported(world: World) -> None:
    world.store.register_source(WS, "board:main", kind="board", display_name="Board")
    env = world.broker.search(world.ctx(), "owls")
    status = {s.source_id: s for s in env.source_status}["board:main"]
    assert env.partial and status.state == SourceState.UNAVAILABLE
    assert status.error is not None and "board" in status.error.message


def test_unexpected_adapter_error_is_an_error_status(world: World) -> None:
    add_fake(world, "fake:bug", lambda: FakeAdapter("fake:bug", error=RuntimeError("boom")))
    env = world.broker.search(world.ctx(), "owls")
    status = {s.source_id: s for s in env.source_status}["fake:bug"]
    assert status.state == SourceState.ERROR and status.error is not None
    assert status.error.code == "internal_error" and "boom" not in status.error.message


def test_every_selected_source_failing_is_unavailable_not_empty(world: World) -> None:
    add_fake(world, "fake:down", lambda: FakeAdapter("fake:down", error=UnavailableError("down")))
    with pytest.raises(UnavailableError) as exc:
        world.broker.search(world.ctx(source_ids=["fake:down"]), "owls")
    assert exc.value.details["source_status"][0]["state"] == "unavailable"


def test_vector_without_an_index_is_an_error_never_fts(world: World) -> None:

    world.rebuild_broker(replace(world.backend, embeddings=None, embeddings_error="not configured"))
    with pytest.raises(UnavailableError) as exc:
        world.broker.search(world.ctx(mode="vector"), "owls")
    assert "vector" in exc.value.message
    assert world.broker.search(world.ctx(mode="fts"), "owls").hits


def test_vector_index_missing_at_search_time_is_unavailable(world: World) -> None:
    assert world.backend.embeddings is not None
    world.backend.embeddings.db_path.unlink()
    with pytest.raises(UnavailableError) as exc:
        world.broker.search(world.ctx(mode="hybrid"), "owls")
    status = exc.value.details["source_status"][0]
    assert status["source_id"] == CORPUS_SOURCE_ID and status["state"] == "unavailable"


def test_source_missing_a_mode_is_partial_when_others_answer(world: World) -> None:
    add_fake(world, "fake:fts", lambda: FakeAdapter("fake:fts", modes=("fts",)))
    env = world.broker.search(world.ctx(mode="vector"), "owls")
    status = {s.source_id: s for s in env.source_status}
    assert env.partial and status["fake:fts"].state == SourceState.UNAVAILABLE
    assert status[CORPUS_SOURCE_ID].state == SourceState.OK


def test_per_source_timeout_is_enforced(world: World) -> None:
    gate = threading.Event()
    add_fake(world, "fake:slow", lambda: FakeAdapter("fake:slow", gate=gate))
    try:
        env = world.broker.search(world.ctx(timeout_ms=200), "owls")
    finally:
        gate.set()
    status = {s.source_id: s for s in env.source_status}
    assert env.partial and status["fake:slow"].state == SourceState.TIMEOUT
    assert status["fake:slow"].error is not None and status["fake:slow"].error.code == "timeout"
    assert paths_of(env) == [rid("libs/b/owl.md")]


def test_closed_broker_is_unavailable(world: World) -> None:
    world.broker.close()
    with pytest.raises(UnavailableError):
        world.broker.search(world.ctx(), "owls")


# ── open ─────────────────────────────────────────────────────────────


def test_open_returns_the_exact_verified_revision(world: World) -> None:
    hit = world.broker.search(world.ctx(), "owls").hits[0]
    assert hit.revision_id is not None
    result = world.broker.open(world.ctx(), CORPUS_SOURCE_ID, hit.record_id, hit.revision_id)
    data = (world.corpus / "libs/b/owl.md").read_bytes()
    assert result.verification == VerificationState.VERIFIED
    assert result.content == data.decode() and result.digest == sha256_digest(data)
    assert result.media_type == "text/markdown" and result.provenance_id == "entry:lib-b"
    current = world.broker.open(world.ctx(), CORPUS_SOURCE_ID, hit.record_id)
    assert current.revision_id == hit.revision_id
    located = world.broker.open(
        world.ctx(),
        CORPUS_SOURCE_ID,
        hit.record_id,
        hit.revision_id,
        TextLocator(start_byte=0, end_byte=6),
    )
    assert located.locator == TextLocator(start_byte=0, end_byte=6)
    with pytest.raises(InvalidInputError):
        world.broker.open(
            world.ctx(),
            CORPUS_SOURCE_ID,
            hit.record_id,
            hit.revision_id,
            TextLocator(start_byte=0, end_byte=10_000),
        )


def test_open_never_substitutes_the_latest_revision(world: World) -> None:
    hit = world.broker.search(world.ctx(), "owls").hits[0]
    assert hit.revision_id is not None
    (world.corpus / "libs/b/owl.md").write_text("# Owls\n\nEdited.\n", encoding="utf-8")
    stale = world.broker.open(world.ctx(), CORPUS_SOURCE_ID, hit.record_id, hit.revision_id)
    assert stale.verification == VerificationState.UNSUPPORTED
    assert stale.content is None and stale.revision_id == hit.revision_id
    (world.corpus / "libs/b/owl.md").unlink()
    gone = world.broker.open(world.ctx(), CORPUS_SOURCE_ID, hit.record_id, hit.revision_id)
    assert gone.verification == VerificationState.UNAVAILABLE and gone.content is None
    with pytest.raises(UnavailableError):
        world.broker.open(world.ctx(), CORPUS_SOURCE_ID, hit.record_id)


def test_open_unknown_or_out_of_scope_records(world: World) -> None:
    with pytest.raises(UnknownScopeError):
        world.broker.open(world.ctx(), CORPUS_SOURCE_ID, "file-missing")
    with pytest.raises(UnknownScopeError):
        world.broker.open(world.ctx(), "corpus:nope", "file-missing")
    with pytest.raises(UnknownScopeError):
        world.broker.open(world.ctx(source_ids=[]), CORPUS_SOURCE_ID, rid("libs/b/owl.md"))
    with pytest.raises(UnknownScopeError):
        world.broker.open(world.ctx(tags=["absent"]), CORPUS_SOURCE_ID, rid("libs/b/owl.md"))


# ── project context ──────────────────────────────────────────────────


def _project_context_root(root: Path, docs: dict[str, list[tuple[str, str]]]) -> None:
    """One root whose registry holds every fingerprint of ``docs`` under the name ``app``.

    Projects sharing a name share one content DB file, so the broker must filter
    by fingerprint to keep them apart.
    """
    from knowledge_engine.project_context import db as pddb
    from knowledge_engine.project_context.config import ProjectContextConfig
    from knowledge_engine.project_context.paths import (
        canonical_root,
        fingerprint_db_path,
        project_db_path,
    )

    cfg = ProjectContextConfig()
    root.mkdir(parents=True, exist_ok=True)
    now = "2026-10-30T00:00:00Z"
    registry = pddb.connect(fingerprint_db_path(root, cfg))
    pddb.apply_registry_migrations(registry)
    content = pddb.connect(project_db_path(root, cfg, "app"))
    pddb.apply_project_migrations(content)
    for fp, items in docs.items():
        registry.execute(
            "INSERT INTO projects (project_fp, name, root_path, created_at, updated_at)"
            " VALUES (?, 'app', ?, ?, ?)",
            (fp, canonical_root(root), now, now),
        )
        for doc_id, body in items:
            content.execute(
                "INSERT INTO project_docs (record_id, project_fp, branch_fp, category,"
                " content_hash, created_at, updated_at, source_path, summary)"
                " VALUES (?, ?, 'br_main', 'doc', 'h', ?, ?, ?, ?)",
                (doc_id, fp, now, now, f"docs/{doc_id}.md", body[:40]),
            )
            content.execute(
                "INSERT INTO project_doc_bodies (record_id, searchable_body) VALUES (?, ?)",
                (doc_id, body),
            )
            rowid = content.execute(
                "SELECT rowid FROM project_docs WHERE record_id = ?", (doc_id,)
            ).fetchone()[0]
            content.execute(
                "INSERT INTO project_docs_fts (rowid, searchable_body, summary) VALUES (?, ?, ?)",
                (rowid, body, body[:40]),
            )
    registry.commit()
    content.commit()
    registry.close()
    content.close()


PC = source_id_for(WS)
BODY = "the identical deployment runbook for the harbour service"


@pytest.fixture
def linked(world: World) -> World:
    _project_context_root(
        world.root / "repo",
        {"proj_alpha": [("a1", BODY)], "proj_beta": [("b1", BODY), ("b2", "beta only owl notes")]},
    )
    world.store.create_project(WS, "App", project_id="alpha")
    world.store.create_project(WS, "App", project_id="beta")
    link_project(world.store, WS, "alpha", str(world.root / "repo"), project_fp="proj_alpha")
    link_project(world.store, WS, "beta", str(world.root / "repo"), project_fp="proj_beta")
    return world


def test_project_context_projects_sharing_a_store_never_leak(linked: World) -> None:
    alpha = linked.broker.search(
        linked.ctx(project_id="alpha", source_ids=[PC]), "deployment runbook"
    )
    beta = linked.broker.search(
        linked.ctx(project_id="beta", source_ids=[PC]), "deployment runbook"
    )
    assert [h.record_id for h in alpha.hits] == ["alpha:a1"]
    assert [h.record_id for h in beta.hits] == ["beta:b1"]
    assert alpha.hits[0].scope.project_id == "alpha"
    assert alpha.hits[0].provenance_id == "project_context:proj_alpha"
    both = linked.broker.search(linked.ctx(source_ids=[PC]), "deployment runbook")
    assert sorted(h.record_id for h in both.hits) == ["alpha:a1", "beta:b1"]
    assert all(h.ranking.score_kind == "rrf" for h in both.hits)
    # An explicit project query never returns workspace-wide corpus content ...
    mixed = linked.broker.search(linked.ctx(project_id="alpha"), "owls")
    assert mixed.hits == ()
    # ... unless include_workspace asks for it.
    widened = linked.broker.search(linked.ctx(project_id="alpha", include_workspace=True), "owls")
    assert [h.source_id for h in widened.hits] == [CORPUS_SOURCE_ID]
    # Collection/tag constraints exclude unclassified project-context documents.
    linked.store.create_collection(WS, "C", collection_id="c")
    assert (
        linked.broker.search(linked.ctx(collection_ids=["c"], source_ids=[PC]), "runbook").hits
        == ()
    )


def test_project_context_open_and_revisions(linked: World) -> None:
    hit = linked.broker.search(linked.ctx(project_id="alpha", source_ids=[PC]), "runbook").hits[0]
    assert hit.revision_id is not None
    opened = linked.broker.open(linked.ctx(project_id="alpha"), PC, hit.record_id, hit.revision_id)
    assert opened.verification == VerificationState.VERIFIED and opened.content == BODY
    latest = linked.broker.open(linked.ctx(), PC, hit.record_id)
    assert latest.revision_id == hit.revision_id
    with pytest.raises(UnknownScopeError):
        linked.broker.open(linked.ctx(project_id="beta"), PC, hit.record_id)
    with pytest.raises(UnknownScopeError):
        linked.broker.open(linked.ctx(), PC, "beta:a1")  # alpha's document under beta's id
    with pytest.raises(UnknownScopeError):
        linked.broker.open(linked.ctx(), PC, "gamma:a1")


def test_project_context_full_content_gate(linked: World, monkeypatch: pytest.MonkeyPatch) -> None:
    toml = linked.root / "ke.toml"
    toml.write_text("[project_context.mcp]\nallow_full_content = false\n", encoding="utf-8")
    monkeypatch.setenv("KE_CONFIG_PATH", str(toml))
    with pytest.raises(DeniedError):
        linked.broker.open(linked.ctx(), PC, "alpha:a1")


def test_project_context_vector_mode_and_broken_link(linked: World) -> None:
    env = linked.broker.search(linked.ctx(mode="vector"), "owls")
    status = {s.source_id: s for s in env.source_status}
    assert env.partial and status[PC].state == SourceState.UNAVAILABLE
    (linked.root / "repo" / ".knowledge-engine" / "project-fingerprints.sqlite").unlink()
    with pytest.raises(UnavailableError):
        linked.broker.search(linked.ctx(source_ids=[PC]), "runbook")
    env = linked.broker.search(linked.ctx(), "owls")
    assert env.partial and {s.source_id: s.state for s in env.source_status}[PC] == (
        SourceState.UNAVAILABLE
    )


def test_project_context_link_validation(world: World) -> None:
    _project_context_root(world.root / "repo", {"proj_one": [("d1", "text")]})
    world.store.create_project(WS, "One", project_id="one")
    world.store.create_project(WS, "Two", project_id="two")
    with pytest.raises(InvalidInputError):
        link_project(world.store, WS, "one", "relative/path")
    with pytest.raises(UnknownScopeError):
        link_project(world.store, WS, "one", str(world.root / "repo"), project_fp="proj_nope")
    with pytest.raises(UnavailableError):
        link_project(world.store, WS, "one", str(world.root))  # no registry at that root
    linked = link_project(world.store, WS, "one", str(world.root / "repo"), project_fp="proj_one")
    assert linked == {
        "source_id": PC,
        "workspace_id": WS,
        "project_id": "one",
        "project_fp": "proj_one",
        "linked": True,
    }
    with pytest.raises(InvalidInputError):
        link_project(world.store, WS, "two", str(world.root / "repo"), project_fp="proj_one")
    config = world.store.source_config(PC)
    assert set(config["links"]) == {"one"}
    assert unlink_project(world.store, WS, "one")["linked"] is False
    assert world.store.source_config(PC)["links"] == {}


def test_store_snapshot_and_source_config(world: World) -> None:
    snapshot = world.store.snapshot(world.ctx(tags=["none"]))
    assert snapshot.records == {} and CORPUS_SOURCE_ID in snapshot.record_sources
    with pytest.raises(InvalidInputError):
        world.store.set_source_config(CORPUS_SOURCE_ID, {"x": "y" * 300_000})
    assert world.store.set_source_config(CORPUS_SOURCE_ID, {"a": 1}) == {"a": 1}
    assert world.store.source_config(CORPUS_SOURCE_ID) == {"a": 1}


def test_filtered_fts_scan_cap_is_reported(world: World) -> None:
    results, capped = world.backend.indexer.search_where(
        "fox", limit=5, accept=lambda path: "weak" in path, max_scan=3
    )
    assert capped and len(results) <= 3
    results, capped = world.backend.indexer.search_where("fox", limit=5, accept=lambda p: True)
    assert not capped and len(results) == 5
    with pytest.raises(ValueError):
        world.backend.indexer.search_where("fox", limit=0, accept=lambda p: True)


def test_snapshot_json_round_trip(world: World) -> None:
    env = world.broker.search(world.ctx(), "owls")
    assert json.loads(env.model_dump_json())["contract_version"] == 1


def test_unknown_source_config_is_unknown_scope(world: World) -> None:
    with pytest.raises(UnknownScopeError):
        world.store.source_config("corpus:absent")
    with pytest.raises(UnknownScopeError):
        world.store.set_source_config("corpus:absent", {})


def test_open_serves_recorded_revisions_from_the_evidence_store(world: World) -> None:
    from knowledge_engine.knowledge.evidence.snapshot import snapshot
    from knowledge_engine.knowledge.evidence.store import EvidenceStore

    evidence = EvidenceStore(world.store)
    world.rebuild_broker(replace(world.backend, evidence=evidence))
    owl = world.corpus / "libs/b/owl.md"
    original = owl.read_text(encoding="utf-8")

    def record() -> None:
        snapshot(
            evidence,
            corpus_root=world.corpus,
            registry_path=world.corpus / "registry.json",
            apply=True,
        )

    record()
    first = world.broker.search(world.ctx(), "owls").hits[0]
    assert first.revision_id is not None

    owl.write_text("# Owls\n\nOwls hunt mice at night.\n", encoding="utf-8")
    # The file changed but no newer revision was recorded: the store still calls it current.
    current = world.broker.open(world.ctx(), CORPUS_SOURCE_ID, first.record_id, first.revision_id)
    assert current.verification == VerificationState.VERIFIED
    assert current.content == original

    record()
    stale = world.broker.open(world.ctx(), CORPUS_SOURCE_ID, first.record_id, first.revision_id)
    assert stale.verification == VerificationState.STALE
    assert stale.content == original and stale.revision_id == first.revision_id
    with pytest.raises(UnknownScopeError):
        world.broker.open(world.ctx(project_id="beta"), CORPUS_SOURCE_ID, first.record_id)

    # A revision the store never recorded is still refused, never replaced by the file.
    owl.write_text("# Owls\n\nThird version.\n", encoding="utf-8")
    third = world.broker.open(world.ctx(), CORPUS_SOURCE_ID, first.record_id)
    assert third.verification == VerificationState.VERIFIED
    owl.write_text("# Owls\n\nFourth version.\n", encoding="utf-8")
    never = world.broker.open(world.ctx(), CORPUS_SOURCE_ID, first.record_id, third.revision_id)
    assert never.verification == VerificationState.UNSUPPORTED and never.content is None

    owl.unlink()
    gone = world.broker.open(world.ctx(), CORPUS_SOURCE_ID, first.record_id, first.revision_id)
    assert gone.verification == VerificationState.UNAVAILABLE and gone.content is None
