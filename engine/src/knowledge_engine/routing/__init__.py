"""Provider abstraction for LLM routing."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class GenerationRequest:
    prompt: str
    model: str | None = None
    max_tokens: int = 1024
    temperature: float = 0.2
    metadata: dict[str, Any] | None = None


@dataclass
class GenerationResponse:
    text: str
    provider: str
    model: str
    usage: dict[str, Any] | None = None


class Provider(ABC):
    """Abstract provider. Implementations live in cloud.py / routing_local/."""

    name: str = "abstract"

    @abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResponse:
        ...

    @abstractmethod
    def available(self) -> bool:
        ...


class ProviderRegistry:
    """In-memory provider registry. First available provider wins by default."""

    def __init__(self) -> None:
        self._providers: list[Provider] = []

    def register(self, provider: Provider) -> None:
        self._providers.append(provider)

    def list(self) -> list[str]:
        return [p.name for p in self._providers]

    def select(self, preferred: str | None = None) -> Provider | None:
        if preferred:
            for p in self._providers:
                if p.name == preferred and p.available():
                    return p
        for p in self._providers:
            if p.available():
                return p
        return None
