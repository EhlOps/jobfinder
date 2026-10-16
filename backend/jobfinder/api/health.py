"""Liveness and readiness.

    /healthz  the process is up; no I/O (liveness: a failing database must not restart the api)
    /readyz   safe to send traffic: the database answers and its schema revision equals this image's
              alembic head, so a pod never serves against an unmigrated (or newer) schema (#43)

The head is read once, when the app is created, from the alembic scripts shipped next to the package
(backend/alembic.ini and backend/alembic/ in the repo, /app/ in the api image).
"""
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import Request
from fastapi.responses import JSONResponse

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_heads(backend_dir: Path = BACKEND_DIR) -> frozenset[str]:
    """The revision(s) `alembic upgrade head` ends at. Empty when the scripts are not shipped."""
    ini = backend_dir / "alembic.ini"
    if not ini.is_file():
        return frozenset()
    cfg = Config(str(ini))
    # The ini's script_location is relative to the working directory; pin it to the ini's own folder.
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    return frozenset(ScriptDirectory.from_config(cfg).get_heads())


async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _not_ready(reason: str) -> JSONResponse:
    return JSONResponse({"status": "unavailable", "reason": reason}, status_code=503)


async def readyz(request: Request) -> JSONResponse:
    heads: frozenset[str] = request.app.state.alembic_heads
    if not heads:
        return _not_ready("no alembic scripts in this image")
    try:
        async with request.app.state.ctx.sessions() as s:
            await s.execute(sa.text("select 1"))
            # to_regclass instead of selecting straight away: a missing table is an answer, not an error.
            if await s.scalar(sa.text("select to_regclass('alembic_version')")) is None:
                return _not_ready("database is not migrated (no alembic_version table)")
            rows = await s.scalars(sa.text("select version_num from alembic_version"))
            current = frozenset(rows.all())
    except Exception as e:  # any failure to reach the database means "not ready"
        return _not_ready(f"database unreachable: {type(e).__name__}")
    if current != heads:
        have, want = ",".join(sorted(current)) or "nothing", ",".join(sorted(heads))
        return _not_ready(f"schema at {have}, image expects {want}")
    return JSONResponse({"status": "ok", "revision": ",".join(sorted(heads))})
