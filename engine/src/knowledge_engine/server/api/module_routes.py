"""Module admin routes under ``/modules`` (guarded like the board's admin routes).

Every route requires a trusted peer **and** a board key holding
``board:admin`` (``X-Board-Key``), exactly like ``/board/admin/*``: the gate
runs before the body is parsed. All routes call the one shared
:class:`~knowledge_engine.modules.dispatcher.ModuleHost`, so HTTP, the
``knowledge-engine module`` CLI and the MCP module tools behave the same.
Failures are typed ``KnowledgeError`` answers
(``{"detail", "type", "error": {...}}``).

Registration over HTTP only accepts a directory **name** inside
``KE_MODULES_DIR``; free filesystem paths are a CLI-only operator action.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ...modules.dispatcher import ModuleHost

Capability = Literal["read", "write", "network", "process"]


def modules_admin_gate(request: Request) -> str:
    """Trusted peer + ``board:admin`` key (the board's ADMIN gate); returns the key id."""
    from ...agent_board.http.trust import admin_gate  # noqa: PLC0415 - board is optional

    return admin_gate(request)


def get_module_host(request: Request) -> ModuleHost:
    host: ModuleHost | None = getattr(request.app.state, "modules", None)
    if host is None:
        raise HTTPException(503, "module catalog unavailable")
    return host


AdminKey = Annotated[str, Depends(modules_admin_gate)]
HostDep = Annotated[ModuleHost, Depends(get_module_host)]

router = APIRouter(dependencies=[Depends(modules_admin_gate)])


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterBody(_Body):
    directory: str = Field(
        min_length=1, max_length=255, description="Sub-directory name inside KE_MODULES_DIR."
    )


class ConfigureBody(_Body):
    settings: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(
        default_factory=dict, description="secret name -> environment variable NAME (never a value)"
    )


class EnableBody(_Body):
    grants: list[Capability] | None = Field(
        default=None, description="Capabilities to grant (default: declared ∩ host policy)."
    )


class ScopeBody(_Body):
    workspace_id: str
    project_id: str | None = None


class InvokeBody(_Body):
    input: dict[str, Any] = Field(default_factory=dict)
    scope: ScopeBody | None = Field(default=None, description="Default: the default workspace.")
    timeout_ms: int | None = Field(default=None, ge=1, le=3_600_000)
    effects: list[Capability] | None = Field(
        default=None, description="Effects this call allows, e.g. ['read'] for read-only."
    )
    ingest: bool = Field(default=False, description="Ingest artifacts into the explicit scope.")


class ModuleList(BaseModel):
    modules: list[dict[str, Any]]


class DiscoverList(BaseModel):
    directories: list[dict[str, Any]]


class JobList(BaseModel):
    jobs: list[dict[str, Any]]


@router.get("", response_model=ModuleList)
def list_modules(host: HostDep) -> dict[str, Any]:
    return {"modules": host.list_modules()}


@router.get("/discover", response_model=DiscoverList)
def discover(host: HostDep) -> dict[str, Any]:
    return {"directories": host.discover()}


@router.post("/register", response_model=dict[str, Any])
def register(payload: RegisterBody, host: HostDep) -> dict[str, Any]:
    return host.register_named(payload.directory)


@router.get("/jobs", response_model=JobList)
def list_jobs(host: HostDep, workspace_id: str | None = None) -> dict[str, Any]:
    return {"jobs": host.job_list(workspace_id)}


@router.get("/jobs/{job_id}", response_model=dict[str, Any])
def job_status(job_id: str, host: HostDep, workspace_id: str | None = None) -> dict[str, Any]:
    return host.job_status(job_id, workspace_id)


@router.post("/jobs/{job_id}/cancel", response_model=dict[str, Any])
def job_cancel(job_id: str, host: HostDep, workspace_id: str | None = None) -> dict[str, Any]:
    return host.job_cancel(job_id, workspace_id)


@router.get("/{module_id}", response_model=dict[str, Any])
def module_status(module_id: str, host: HostDep) -> dict[str, Any]:
    return host.status(module_id)


@router.post("/{module_id}/health", response_model=dict[str, Any])
def module_probe(module_id: str, host: HostDep) -> dict[str, Any]:
    return host.status(module_id, probe=True)


@router.put("/{module_id}/configuration", response_model=dict[str, Any])
def configure(module_id: str, payload: ConfigureBody, host: HostDep) -> dict[str, Any]:
    return host.configure(module_id, payload.settings, payload.secrets)


@router.post("/{module_id}/enable", response_model=dict[str, Any])
def enable(module_id: str, host: HostDep, payload: EnableBody | None = None) -> dict[str, Any]:
    return host.enable(module_id, payload.grants if payload else None)


@router.post("/{module_id}/disable", response_model=dict[str, Any])
def disable(module_id: str, host: HostDep) -> dict[str, Any]:
    return host.disable(module_id)


@router.post("/{module_id}/tools/{tool}/invoke", response_model=dict[str, Any])
def invoke(
    module_id: str, tool: str, payload: InvokeBody, host: HostDep, key_id: AdminKey
) -> dict[str, Any]:
    return host.invoke(
        module_id,
        tool,
        payload.input,
        scope=payload.scope.model_dump() if payload.scope else None,
        surface="http",
        principal=f"key:{key_id}" if key_id else "http",
        timeout_ms=payload.timeout_ms,
        effects=payload.effects,
        ingest=payload.ingest,
    )


__all__ = ["get_module_host", "modules_admin_gate", "router"]
