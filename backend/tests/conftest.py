import os

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Needs a reachable Postgres (docker compose up -d db publishes it on :5433).
ADMIN_URL = os.environ.get(
    "TEST_ADMIN_URL", "postgresql+asyncpg://jobfinder:change-me@localhost:5433/postgres"
)
TEST_DB = os.environ.get("TEST_DB", "jobfinder_test")
TEST_URL = ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB}"

os.environ["DATABASE_URL"] = TEST_URL
os.environ["SECRET_KEY"] = "test-secret-key-0123456789-abcdefghijklmnop"

from jobfinder.db import Base, get_db
from jobfinder.main import app


@pytest_asyncio.fixture
async def engine():
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as conn:
        exists = await conn.scalar(sa.text("select 1 from pg_database where datname=:n"), {"n": TEST_DB})
        if not exists:
            await conn.execute(sa.text(f'create database "{TEST_DB}"'))
    await admin.dispose()

    eng = create_async_engine(TEST_URL, poolclass=NullPool)
    async with eng.begin() as conn:
        # Reset the whole schema (not drop_all) so tables from older migrations can't linger.
        await conn.execute(sa.text("drop schema public cascade"))
        await conn.execute(sa.text("create schema public"))
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def client(engine, tmp_path, monkeypatch):
    from jobfinder.config import get_settings

    monkeypatch.setattr(get_settings(), "store_dir", str(tmp_path / "store"))
    maker = async_sessionmaker(engine, expire_on_commit=False)

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
    monkeypatch.setattr(s, "admin_emails", "")


@pytest.fixture
def creds():
    return {"email": "sam@example.com", "password": "correct-horse-battery"}


@pytest_asyncio.fixture
async def make_account(client, engine):
    """Insert an active (activated) user + profile directly, then optionally log in through the real endpoint."""
    from jobfinder.auth.security import hash_password
    from jobfinder.models import Profile, User

    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def make(email="sam@example.com", password="correct-horse-battery", *, admin=False, login=True, pending=False):
        from datetime import UTC, datetime

        from jobfinder.config import get_settings

        if admin:
            s = get_settings()
            s.admin_emails = ",".join(x for x in (s.admin_emails, email) if x)
        async with maker() as db:
            if pending:
                u = User(email=email.lower())
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
