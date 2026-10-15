"""Snapshot of the public API surface: the OpenAPI schema and the route table.

Every refactor PR must keep ``tests/snapshots/openapi.json`` and ``tests/snapshots/routes.json``
byte-identical. If one of these tests fails, the API changed. Only regenerate the snapshots when
the change is intentional, and call it out in the PR:

    UPDATE_SNAPSHOTS=1 uv run pytest tests/test_openapi_snapshot.py

The route table records the endpoint *name* (not its module), so moving a handler to another
module does not change the snapshot. Renaming it, or changing its path, method, status code or
response model, does. Note that FastAPI names a schema component after its module when two
models share a class name (e.g. ``jobfinder__api__links__LinkOut``), so moving such a model
changes ``openapi.json``: that is a real change to the published schema, not noise.

The exact output also depends on the FastAPI and Pydantic versions pinned in ``uv.lock``. A
dependency bump that changes schema generation needs a deliberate regeneration in that PR.

These tests only import the app (conftest supplies the env vars it needs at import); they never
touch the database.
"""

import difflib
import json
import os
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import pytest
from fastapi.routing import APIRoute, iter_route_contexts

from jobfinder.main import app

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
REGEN_CMD = "UPDATE_SNAPSHOTS=1 uv run pytest tests/test_openapi_snapshot.py"


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _type_name(tp: Any) -> str | None:
    """A module-independent name for a response model, e.g. ``list[JobOut]``."""
    if tp is None:
        return None
    origin, args = get_origin(tp), get_args(tp)
    if origin is None or not args:
        return getattr(tp, "__qualname__", None) or repr(tp)
    if origin is Literal:
        inner = ", ".join(repr(a) for a in args)
    else:
        inner = ", ".join(_type_name(a) or "None" for a in args)
    return f"{getattr(origin, '__qualname__', None) or repr(origin)}[{inner}]"


def route_table() -> list[dict[str, Any]]:
    rows = []
    for route in iter_route_contexts(app.routes):
        original = route.original_route
        row: dict[str, Any] = {
            "kind": type(original).__name__,
            "methods": sorted(getattr(route, "methods", None) or []),
            "path": getattr(route, "path", None),
            "name": getattr(route, "name", None),
        }
        if isinstance(original, APIRoute):
            # Read through the route context so include_router() options are reflected.
            row["status_code"] = route.status_code
            row["response_model"] = _type_name(route.response_model)
            row["include_in_schema"] = route.include_in_schema
        rows.append(row)
    rows.sort(key=lambda r: (r["path"] or "", r["methods"], r["name"] or ""))
    return rows


def _check(filename: str, data: Any) -> None:
    path = SNAPSHOT_DIR / filename
    actual = _dump(data)
    if os.environ.get("UPDATE_SNAPSHOTS", "").lower() in {"1", "true", "yes"}:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        pytest.skip(f"UPDATE_SNAPSHOTS is set: wrote {path.name}, nothing was checked")
    if not path.exists():
        pytest.fail(f"Snapshot {path} is missing. Create it with:\n    {REGEN_CMD}")
    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"snapshots/{filename} (committed)",
                tofile=f"snapshots/{filename} (current app)",
            )
        )
        pytest.fail(
            f"The API surface changed ({filename}). If this is intentional, regenerate the "
            f"snapshots and commit them:\n    {REGEN_CMD}\n\n{diff}",
            pytrace=False,
        )


def test_openapi_snapshot():
    _check("openapi.json", app.openapi())


def test_route_table_snapshot():
    _check("routes.json", route_table())
