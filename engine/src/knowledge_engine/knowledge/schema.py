"""JSON Schema export of the public knowledge contracts (pinned in a test snapshot).

Kept apart from :mod:`.contracts` so the contract models and the adapter
protocol (:mod:`.adapter`, which imports the models) never import each other.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .adapter import AdapterCapabilities, AdapterSearchResult
from .contracts import (
    CONTRACT_VERSION,
    PUBLIC_MODELS,
    SEARCH_MODES,
    CurationStatus,
    ResearchType,
    SourceState,
    VerificationState,
)
from .errors import ERROR_CODES


def contract_json_schema() -> dict[str, Any]:
    """JSON Schema of every public model plus the controlled vocabularies (deterministic)."""
    models: dict[str, Any] = {}
    exported: list[type[BaseModel]] = [*PUBLIC_MODELS, AdapterCapabilities, AdapterSearchResult]
    for model in exported:
        models[model.__name__] = model.model_json_schema(mode="validation")
    return {
        "contract_version": CONTRACT_VERSION,
        "vocabularies": {
            "ResearchType": [item.value for item in ResearchType],
            "CurationStatus": [item.value for item in CurationStatus],
            "VerificationState": [item.value for item in VerificationState],
            "SourceState": [item.value for item in SourceState],
            "SearchMode": list(SEARCH_MODES),
            "ErrorCode": list(ERROR_CODES),
        },
        "models": models,
    }


__all__ = ["contract_json_schema"]
