"""Liveness and readiness.

    /healthz  the process is up; no I/O (liveness: a failing database must not restart the api)
    /readyz   safe to send traffic: the database answers and its schema is not behind this image's
              alembic head, so a pod never serves against an unmigrated schema (#43)

"Behind" means the database is at a revision this image knows that is not its head (or has no
alembic_version at all). A revision the image doesn't know is one a newer image migrated to: the old
pods keep serving while that newer image rolls out, instead of every pod dropping out of rotation the
moment the migrate Job finishes (migrations are expected to stay backward compatible for one release).

The scripts are read once per process, from the alembic files shipped next to the package
(backend/alembic.ini and backend/alembic/ in the repo, /app/ in the api image).
"""
import asyncio
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import Request
from fastapi.responses import JSONResponse

from jobfinder.services import health as service

BACKEND_DIR = Path(__file__).resolve().parents[2]
# Well under the probe's 3s timeout, so a hung database can't pile up checks holding pool slots.
CHECK_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class Revisions:
    heads: frozenset[str]   # where `alembic upgrade head` ends
    known: frozenset[str]   # every revision this image ships


@cache
def alembic_revisions(backend_dir: Path = BACKEND_DIR) -> Revisions:
    """This image's alembic revisions; empty when the scripts are not shipped. Cached: loading the
    scripts imports every migration and prepends to sys.path."""
    ini = backend_dir / "alembic.ini"
    if not ini.is_file():
        return Revisions(frozenset(), frozenset())
    cfg = Config(str(ini))
    # The ini's script_location is relative to the working directory; pin it to the ini's own folder.
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    scripts = ScriptDirectory.from_config(cfg)
    return Revisions(
        heads=frozenset(scripts.get_heads()),
        known=frozenset(r.revision for r in scripts.walk_revisions()),
    )


async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _not_ready(reason: str) -> JSONResponse:
    return JSONResponse({"status": "unavailable", "reason": reason}, status_code=503)


async def _db_revisions(request: Request) -> frozenset[str] | None:
    """The database's alembic revisions; None when it has no alembic_version table."""
    async with request.app.state.ctx.sessions() as s:
        return await service.schema_revisions(s)


async def readyz(request: Request) -> JSONResponse:
    revs: Revisions = request.app.state.alembic
    if not revs.heads:
        return _not_ready("no alembic scripts in this image")
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            current = await _db_revisions(request)
    except Exception as e:  # any failure to reach the database (or a timeout) means "not ready"
        return _not_ready(f"database unreachable: {type(e).__name__}")
    if current is None:
        return _not_ready("database is not migrated (no alembic_version table)")
    have, want = ",".join(sorted(current)) or "nothing", ",".join(sorted(revs.heads))
    if current == revs.heads:
        return JSONResponse({"status": "ok", "revision": want})
    if current and not current <= revs.known:
        return JSONResponse({"status": "ok", "revision": have, "note": f"schema newer than {want}"})
    return _not_ready(f"schema at {have}, image expects {want}")
