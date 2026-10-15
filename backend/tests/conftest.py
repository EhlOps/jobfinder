import asyncio
import os
from pathlib import Path
from urllib.parse import quote

import pytest
import pytest_asyncio
import sqlalchemy as sa
from dotenv import dotenv_values
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Needs a reachable Postgres (docker compose up -d db publishes it on :5433).
# Credentials come from the repo-root .env (the same POSTGRES_* the compose db is created with);
# TEST_ADMIN_URL overrides everything, and real environment variables win over the file.
_env = {**dotenv_values(Path(__file__).resolve().parents[2] / ".env"), **os.environ}
ADMIN_URL = os.environ.get("TEST_ADMIN_URL") or (
    f"postgresql+asyncpg://{quote(_env.get('POSTGRES_USER') or 'jobfinder', safe='')}"
    f":{quote(_env.get('POSTGRES_PASSWORD') or 'change-me', safe='')}"
    f"@localhost:{os.environ.get('TEST_DB_PORT') or _env.get('DB_PORT') or '5433'}/postgres"
)
TEST_DB = os.environ.get("TEST_DB", "jobfinder_test")
# One database per xdist worker ("master" when not distributed), so workers never share rows.
TEST_DB_NAME = f"{TEST_DB}_{os.environ.get('PYTEST_XDIST_WORKER', 'master')}"
# real_commit tests get a database of their own, reset per test, so their commits can't leak.
# The app's own engine points there too: code that slips past the test session (e.g. run_task's
# import-time default session_factory) never sees the test's rows and can't commit into the
# shared database; whatever it writes is wiped by the next real_commit reset.
RC_DB_NAME = f"{TEST_DB_NAME}_rc"
# Postgres silently truncates longer names, which would break the "does it exist" check.
assert len(RC_DB_NAME) <= 63, f"TEST_DB={TEST_DB!r} is too long for a per-worker database name"
TEST_URL = ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB_NAME}"
RC_URL = ADMIN_URL.rsplit("/", 1)[0] + f"/{RC_DB_NAME}"

os.environ["DATABASE_URL"] = RC_URL
os.environ["SECRET_KEY"] = "test-secret-key-0123456789-abcdefghijklmnop"
# An https value in backend/.env would make the session cookie Secure, which the http test client drops.
os.environ["PUBLIC_BASE_URL"] = "http://test"

from jobfinder import db as dbmod
from jobfinder import worker
from jobfinder.db import Base, get_db
from jobfinder.main import app


async def _drop_leaked_connections(name):
    # A session an earlier test left open (e.g. in a background task it never cancelled) can sit
    # "idle in transaction" holding locks, and the schema reset would wait on it forever. Each of
    # these databases belongs to one worker, so any other connection to it is such a leak.
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as conn:
        await conn.execute(
            sa.text("select pg_terminate_backend(pid) from pg_stat_activity where datname=:n and pid <> pg_backend_pid()"),
            {"n": name},
        )
    await admin.dispose()


async def _reset_schema(url):
    await _drop_leaked_connections(url.rsplit("/", 1)[1])
    eng = create_async_engine(url, poolclass=NullPool)
    async with eng.begin() as conn:
        # Reset the whole schema (not drop_all) so tables from older migrations can't linger.
        await conn.execute(sa.text("drop schema public cascade"))
        await conn.execute(sa.text("create schema public"))
        await conn.run_sync(Base.metadata.create_all)
    await eng.dispose()


async def _create_databases():
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as conn:
        for name in (TEST_DB_NAME, RC_DB_NAME):
            exists = await conn.scalar(sa.text("select 1 from pg_database where datname=:n"), {"n": name})
            if not exists:
                await conn.execute(sa.text(f'create database "{name}"'))
    await admin.dispose()
    await _reset_schema(TEST_URL)


@pytest.fixture(scope="session")
def _test_databases():
    """Create this worker's databases and build the schema once; after that tests only roll back."""
    # Its own short-lived loop: pytest-asyncio gives each test a fresh one, and nothing here outlives it.
    asyncio.run(_create_databases())


def _route_app_sessions(monkeypatch, m):
    # App code outside a request (worker, CLI, renderer, scheduler) opens SessionLocal itself.
    monkeypatch.setattr(dbmod, "SessionLocal", m)
    monkeypatch.setattr(worker, "SessionLocal", m)


@pytest_asyncio.fixture
async def maker(request, monkeypatch, _test_databases):
    """The one session factory for a test, its HTTP client and the app's own SessionLocal.

    Every session joins a single outer transaction on one connection, so the app's commit() only
    releases a SAVEPOINT and the whole test is rolled back at the end. Sessions that run
    concurrently (asyncio.gather, background loops) can't share one connection's SAVEPOINT stack,
    so tests doing that, or needing commits visible on other connections, are marked real_commit
    and get a real engine on a freshly reset database instead.

    Two differences from production to keep in mind: Postgres now() (server defaults like
    created_at) is fixed for the whole test, and a session opened inside another one's savepoint
    loses its "committed" work if the outer session rolls back.
    """
    if request.node.get_closest_marker("real_commit"):
        await _reset_schema(RC_URL)
        eng = create_async_engine(RC_URL, poolclass=NullPool)
        m = async_sessionmaker(eng, expire_on_commit=False)
        _route_app_sessions(monkeypatch, m)
        yield m
        await eng.dispose()
        return

    eng = create_async_engine(TEST_URL, poolclass=NullPool)
    async with eng.connect() as conn:
        outer = await conn.begin()
        m = async_sessionmaker(conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        _route_app_sessions(monkeypatch, m)
        try:
            yield m
        finally:
            if outer.is_active:
                await outer.rollback()
    await eng.dispose()


@pytest_asyncio.fixture
async def db(maker):
    async with maker() as s:
        yield s


@pytest_asyncio.fixture
async def client(maker, tmp_path, monkeypatch):
    from jobfinder.config import get_settings

    monkeypatch.setattr(get_settings(), "store_dir", str(tmp_path / "store"))

    async def override_db():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def isolated_claude_auth(tmp_path, monkeypatch):
    """Never let a test read or write a real credentials directory."""
    from jobfinder.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "claude_auth_dir", str(tmp_path / "claude-auth"))
    monkeypatch.setattr(s, "claude_code_oauth_token", "")


@pytest.fixture
def creds():
    return {"email": "sam@example.com", "password": "correct-horse-battery"}


@pytest_asyncio.fixture
async def make_account(client, maker):
    """Insert an active (activated) user + profile directly, then optionally log in through the real endpoint."""
    from jobfinder.auth.security import hash_password
    from jobfinder.models import Profile, User

    async def make(email="sam@example.com", password="correct-horse-battery", *, admin=False, login=True, pending=False):
        from datetime import UTC, datetime

        async with maker() as db:
            if pending:
                u = User(email=email.lower(), is_admin=admin)
            else:
                u = User(
                    email=email.lower(), password_hash=hash_password(password), timezone="UTC", digest_hour=8,
                    digest_enabled=True, question_emails_enabled=True, is_admin=admin, activated_at=datetime.now(UTC),
                )
            db.add(u)
            await db.flush()
            if not pending:
                db.add(Profile(user_id=u.id, status={}, background={}, version=1))
            await db.commit()
        if login and not pending:
            r = await client.post("/api/auth/login", json={"email": email, "password": password})
            assert r.status_code == 200, r.text
        return {"email": email.lower(), "password": password}

    return make


@pytest.fixture
def sent_mail(monkeypatch):
    """Collects (to, subject, text) for every email the app tries to send."""
    sent: list[tuple[str, str, str]] = []

    async def fake(to, subject, text, html=None, headers=None):
        sent.append((to, subject, text))

    monkeypatch.setattr("jobfinder.notify.email.send_email", fake)
    return sent


@pytest_asyncio.fixture
async def authed(client, creds, make_account):
    await make_account(creds["email"], creds["password"])
    return client
