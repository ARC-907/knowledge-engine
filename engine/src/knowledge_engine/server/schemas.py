"""Request and response models of the engine's HTTP API (OpenAPI source of truth).

The response models describe the payloads the routes have always returned.
Registry entries and lifecycle state are read from the operator-edited
``registry.json``, so their fields are documented with a JSON schema but
passed through unchanged at runtime (and only the fields a row actually has
are emitted): a hand-written row with an unexpected value type keeps being
served instead of turning into a 500.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from ..core.registry import REGISTRY_KINDS, RegistryKind


def _doc(schema: dict[str, Any]) -> Any:
    return WithJsonSchema(schema)


# ── Health / info ────────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str = Field(description="Always `ok` when the app is serving.")
    version: str


class RegistryCounts(BaseModel):
    libraries: int
    skills: int
    kits: int
    tools: int


class LifecycleState(BaseModel):
    """Corpus lifecycle flags (``registry.json`` → ``lifecycle``)."""

    model_config = ConfigDict(extra="allow")

    watch_enabled: Annotated[
        Any, _doc({"type": "boolean", "description": "Watch the corpus for new folders."})
    ] = None
    auto_register: Annotated[
        Any, _doc({"type": "boolean", "description": "Register new corpus folders automatically."})
    ] = None


class InfoResponse(BaseModel):
    version: str
    corpus_root: str
    data_dir: str
    counts: RegistryCounts
    providers: list[str] = Field(description="Registered generation providers, in selection order.")
    lifecycle: LifecycleState


# ── Registry ─────────────────────────────────────────────────────


class RegistryEntryOut(BaseModel):
    """One registry entry as stored in ``registry.json`` plus its ``kind``."""

    model_config = ConfigDict(extra="allow")

    id: Annotated[Any, _doc({"type": "string"})]
    kind: RegistryKind
    name: Annotated[Any, _doc({"type": "string"})] = None
    path: Annotated[
        Any, _doc({"type": "string", "description": "Relative to the corpus root."})
    ] = None
    enabled: Annotated[Any, _doc({"type": "boolean"})] = None
    auto_registered: Annotated[Any, _doc({"type": "boolean"})] = None
    description: Annotated[Any, _doc({"type": "string"})] = None
    tags: Annotated[Any, _doc({"type": "array", "items": {"type": "string"}})] = None
    created: Annotated[Any, _doc({"type": "number", "description": "Unix time."})] = None
    last_updated: Annotated[Any, _doc({"type": "number", "description": "Unix time."})] = None


class EntryIn(BaseModel):
    id: str = Field(min_length=1)
    kind: RegistryKind
    name: str
    path: str
    enabled: bool = True
    auto_registered: bool = False
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class ToggleIn(BaseModel):
    enabled: bool


class LifecycleIn(BaseModel):
    watch_enabled: bool | None = None
    auto_register: bool | None = None


class RemovedResponse(BaseModel):
    removed: str


# ── Search ───────────────────────────────────────────────────────


class SearchHit(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)

    entry_id: str
    entry_kind: str
    path: str = Field(description="Matched file, relative to the corpus root.")
    snippet: str = Field(description="Match context; hits are wrapped in <mark>...</mark>.")
    score: float = Field(description="BM25 rank (lower is better).")


class SearchResponse(BaseModel):
    query: str
    count: int = Field(description="Number of hits returned.")
    results: list[SearchHit]
    syntax_fallback: bool | None = Field(
        None,
        description=(
            "Present (true) only when `q` was not valid FTS5 syntax and was "
            "searched as one quoted phrase instead."
        ),
    )


class ReindexResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    entries: int = Field(description="Enabled registry entries visited.")
    files: int = Field(description="Files indexed.")
    missing: int = Field(description="Entries whose folder is missing from the corpus.")


# ── Generation ───────────────────────────────────────────────────


class GenIn(BaseModel):
    prompt: str
    provider: str | None = Field(
        None, description="Preferred provider; falls back to the first available one."
    )
    model: str | None = None
    max_tokens: int = Field(1024, ge=1)
    # No upper bound: backends differ (OpenAI caps at 2.0, Ollama does not).
    temperature: float = Field(0.2, ge=0.0)


class GenerateResponse(BaseModel):
    text: str
    provider: str
    model: str
    usage: dict[str, Any] | None = None


# ── Errors ───────────────────────────────────────────────────────


class ErrorResponse(BaseModel):
    """Body of the 400/502/503 answers produced by the global exception handlers."""

    detail: Any
    type: str


__all__ = [
    "REGISTRY_KINDS",
    "EntryIn",
    "ErrorResponse",
    "GenIn",
    "GenerateResponse",
    "HealthResponse",
    "InfoResponse",
    "LifecycleIn",
    "LifecycleState",
    "RegistryCounts",
    "RegistryEntryOut",
    "ReindexResponse",
    "RemovedResponse",
    "SearchHit",
    "SearchResponse",
    "ToggleIn",
]
