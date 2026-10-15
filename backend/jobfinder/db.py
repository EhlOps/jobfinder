"""Shim until #53: database primitives live in jobfinder.core.db. New code imports from there.

`SessionLocal` and `engine` are the process context's (core.context.default_context), built on first
use, never at import. Code outside a request (worker, CLI, renderer, scheduled handlers) still opens
`db.SessionLocal()` itself until the task runtime (#48) hands it a session; tests swap it for the
test database.
"""
from typing import Any

from jobfinder.core.context import default_context
from jobfinder.core.db import Base, DbSession, get_db, side_session

__all__ = ["Base", "DbSession", "get_db", "side_session"]  # plus SessionLocal and engine, below


def __getattr__(name: str) -> Any:
    if name == "SessionLocal":
        return default_context().sessions
    if name == "engine":
        return default_context().engine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
