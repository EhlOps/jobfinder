"""The app factory (`uvicorn jobfinder.api.app:create_app --factory`), the per-request transaction and
the AppError handler (#39)."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from jobfinder.api.app import create_app
from jobfinder.core import errors
from jobfinder.core.config import get_settings
from jobfinder.core.context import AppContext, build_context
from jobfinder.core.db import DbSession
from jobfinder.models import RateEvent

BACKEND = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _uvicorn(port: int, env: dict[str, str], cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "jobfinder.api.app:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def test_uvicorn_factory_serves_healthz():
    """The Dockerfile's command line, against backend/.env.defaults (no database needed for /healthz)."""
    port = _free_port()
    proc = _uvicorn(port, dict(os.environ), BACKEND)
    try:
        deadline = time.monotonic() + 30
        while True:
            assert proc.poll() is None, proc.stdout.read() if proc.stdout else ""
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1)
                break
            except httpx.TransportError:
                assert time.monotonic() < deadline, "uvicorn did not start"
                time.sleep(0.2)
        assert r.json() == {"status": "ok"}
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_uvicorn_factory_refuses_to_start_without_a_key(tmp_path):
    keys = (BACKEND / ".env.defaults").read_text().splitlines()
    (tmp_path / ".env.defaults").write_text("\n".join(k for k in keys if not k.startswith("STORE_DIR=")))
    env = {k: v for k, v in os.environ.items() if k != "STORE_DIR"}
    proc = _uvicorn(_free_port(), env, tmp_path)
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode != 0 and "missing keys: STORE_DIR" in out


def test_create_app_uses_the_process_context_by_default():
    app = create_app()
    assert app.state.ctx.settings is get_settings()
    assert app.title == "JobFinder"


def _app(maker) -> tuple[FastAPI, AppContext]:
    bind = maker.kw["bind"]
    ctx = build_context(get_settings(), engine=bind.engine, sessions=maker)
    app = FastAPI()
    app.state.ctx = ctx
    errors.install(app)

    @app.post("/ok")
    async def ok(db: DbSession):
        db.add(RateEvent(kind="t_ok", key="k"))  # no commit: get_db commits when the handler returns

    @app.post("/fail")
    async def fail(db: DbSession):
        db.add(RateEvent(kind="t_fail", key="k"))
        await db.flush()
        raise HTTPException(409, "no")

    @app.get("/missing")
    async def missing():
        raise errors.NotFound("No such match")

    @app.get("/limited")
    async def limited():
        raise errors.RateLimited()

    return app, ctx


async def _count(maker, kind: str) -> int:
    async with maker() as db:
        return await db.scalar(sa.select(sa.func.count()).select_from(RateEvent).where(RateEvent.kind == kind))


async def test_request_commits_when_the_handler_returns_and_rolls_back_when_it_raises(maker):
    app, ctx = _app(maker)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.post("/ok")).status_code == 200
        assert (await c.post("/fail")).status_code == 409
    await ctx.http.aclose()
    assert await _count(maker, "t_ok") == 1
    assert await _count(maker, "t_fail") == 0


async def test_app_errors_become_detail_and_code(maker):
    app, ctx = _app(maker)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/missing")
        assert (r.status_code, r.json()) == (404, {"detail": "No such match", "code": "not_found"})
        r = await c.get("/limited")
        assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    await ctx.http.aclose()


@pytest.mark.parametrize("cls", [errors.NotFound, errors.Forbidden, errors.Conflict, errors.Invalid])
def test_app_error_defaults(cls):
    e = cls()
    assert isinstance(e, errors.AppError) and str(e) == cls.message and e.status >= 400
