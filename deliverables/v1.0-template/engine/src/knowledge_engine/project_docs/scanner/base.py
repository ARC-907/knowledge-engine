"""Detector base class and registry for the project-docs scanner.

A :class:`Detector` knows how to recognize a particular kind of document under a
project root and emit :class:`~knowledge_engine.project_docs.models.Candidate`
records describing what it found. Detectors register themselves with the
module-level registry via :func:`register_detector`; callers obtain live
instances with :func:`iter_detectors`.

The ``Candidate`` DTO is owned by :mod:`knowledge_engine.project_docs.models`
and is intentionally re-used here rather than redefined.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path

from ..models import Candidate

# Registry of detector classes, populated by ``register_detector``.
_DETECTORS: list[type[Detector]] = []


class Detector(ABC):
    """Recognizes a category of document under a project root.

    Subclasses set ``name`` (a stable identifier recorded on each candidate via
    ``Candidate.detector``) and ``category`` (one of ``schema.CATEGORIES``) and
    implement :meth:`discover`.
    """

    name: str = ""
    category: str = ""

    @abstractmethod
    def discover(self, root: Path, cfg) -> Iterable[Candidate]:
        """Yield candidate documents discovered beneath ``root``.

        Implementations should be side-effect free and must not raise for
        ordinary I/O conditions; the orchestrator treats a raised exception as a
        detector fault and skips the remaining output of that detector.
        """
        raise NotImplementedError


def register_detector(cls: type[Detector]) -> type[Detector]:
    """Class decorator that registers ``cls`` in the detector registry.

    Idempotent; returns the class unchanged so it can be used as a decorator.
    """
    if cls not in _DETECTORS:
        _DETECTORS.append(cls)
    return cls


def iter_detectors() -> list[Detector]:
    """Instantiate and return one instance of every registered detector."""
    return [cls() for cls in _DETECTORS]
