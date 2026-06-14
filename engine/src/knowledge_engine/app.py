"""Compatibility shim. Canonical app factory lives in knowledge_engine.server.app."""
from .server.app import create_app

__all__ = ["create_app"]
