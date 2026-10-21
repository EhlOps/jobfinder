"""Readiness: is this pod safe to send traffic? (api/health.py serves the answer as /readyz.)

Ready means the database answers and its schema is not behind this image's alembic head, so a pod
never serves against an unmigrated schema (#43).

"Behind" means the database is at a revision this image knows that is not its head (or has no
alembic_version at all). A revision the image doesn't know is one a newer image migrated to: the old
pods keep serving while that newer image rolls out, instead of every pod dropping out of rotation the
moment the migrate Job finishes (migrations are expected to stay backward compatible for one release).

The scripts are read once per process, from the alembic files shipped next to the package
(backend/alembic.ini and backend/alembic/ in the repo, /app/ in the api image).
"""
import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from jobfinder.core.db import AsyncSession
from jobfinder.repositories import migrations

BACKEND_DIR = Path(__file__).resolve().parents[2]
# Well under the probe's 3s timeout, so a hung database can't pile up checks holding pool slots.
CHECK_TIMEOUT_SECONDS = 2.0

Sessions = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class Revisions:
    heads: frozenset[str]   # where `alembic upgrade head` ends
    known: frozenset[str]   # every revision this image ships


@dataclass(frozen=True)
class Readiness:
    ready: bool
    revision: str | None = None  # when ready: the schema's revision
    note: str | None = None      # when ready on a newer schema
    reason: str | None = None    # when not ready


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


async def _db_revisions(sessions: Sessions) -> frozenset[str] | None:
    """The database's alembic revisions; None when it has no alembic_version table."""
    async with sessions() as s:
        return await migrations.alembic_revisions(s)


async def readiness(sessions: Sessions, revs: Revisions) -> Readiness:
    """Compare the database's revisions (read on a session from `sessions`) with this image's."""
    if not revs.heads:
        return Readiness(False, reason="no alembic scripts in this image")
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            current = await _db_revisions(sessions)
    except Exception as e:  # any failure to reach the database (or a timeout) means "not ready"
        return Readiness(False, reason=f"database unreachable: {type(e).__name__}")
    if current is None:
        return Readiness(False, reason="database is not migrated (no alembic_version table)")
    have, want = ",".join(sorted(current)) or "nothing", ",".join(sorted(revs.heads))
    if current == revs.heads:
        return Readiness(True, revision=want)
    if current and not current <= revs.known:
        return Readiness(True, revision=have, note=f"schema newer than {want}")
    return Readiness(False, reason=f"schema at {have}, image expects {want}")
