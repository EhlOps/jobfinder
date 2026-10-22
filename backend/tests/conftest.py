import asyncio
import ipaddress
import os
import socket
import sys
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Needs a reachable Postgres: by default the one `uv run jobfinder dev` port-forwards to :5433 (#53).
# TEST_ADMIN_URL overrides it (CI points it at its service container).
DEFAULT_ADMIN_URL = "postgresql+asyncpg://jobfinder:jobfinder@localhost:5433/postgres"
ADMIN_URL = os.environ.get("TEST_ADMIN_URL") or DEFAULT_ADMIN_URL
TEST_DB = os.environ.get("TEST_DB", "jobfinder_test")
# One database per xdist worker ("master" when not distributed), so workers never share rows.
TEST_DB_NAME = f"{TEST_DB}_{os.environ.get('PYTEST_XDIST_WORKER', 'master')}"
# real_commit tests get a database of their own, emptied per test, so their commits can't leak.
# The app's own engine points there too: code that slips past the test session (anything that
# reached the process's default context) never sees the test's rows and can't commit into the
# shared database; whatever it writes is wiped by the next real_commit test's truncate.
RC_DB_NAME = f"{TEST_DB_NAME}_rc"
# Postgres silently truncates longer names, which would break the "does it exist" check.
assert len(RC_DB_NAME) <= 63, f"TEST_DB={TEST_DB!r} is too long for a per-worker database name"
TEST_URL = ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB_NAME}"
RC_URL = ADMIN_URL.rsplit("/", 1)[0] + f"/{RC_DB_NAME}"

# The valkey tests/test_claude_gate.py runs against: by default the one `jobfinder dev` port-forwards to
# :6380; CI points it at its service container. Every other test gets an in-memory gate (below).
TEST_VALKEY_URL = os.environ.get("TEST_VALKEY_URL") or "redis://127.0.0.1:6380/0"

os.environ["DATABASE_URL"] = RC_URL
os.environ["SECRET_KEY"] = "test-secret-key-0123456789-abcdefghijklmnop"
# An https value in backend/.env would make the session cookie Secure, which the http test client drops.
os.environ["PUBLIC_BASE_URL"] = "http://test"

from jobfinder.api.app import create_app
from jobfinder.context import build_context, default_context
from jobfinder.core import context as core_context
from jobfinder.core.config import get_settings
from jobfinder.core.db import Base
from jobfinder.integrations.claude import gate as claude_gate
from jobfinder.storage.documents import DocumentStore
from jobfinder.tasks import ledger
from tests.fakes import LocalGate, MockWeb


async def _drop_leaked_connections(name):
    # A session an earlier test left open (e.g. in a background task it never cancelled) can sit
    # "idle in transaction" holding locks, and the schema reset or truncate would wait on it
    # forever. Each of these databases belongs to one worker, so any other connection is a leak.
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


async def _truncate_all(url):
    """Empty every table (and restart its ids) on a database whose schema is already built."""
    await _drop_leaked_connections(url.rsplit("/", 1)[1])
    eng = create_async_engine(url, poolclass=NullPool)
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with eng.begin() as conn:
        await conn.execute(sa.text(f"truncate {tables} restart identity cascade"))
    await eng.dispose()


async def _create_databases():
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as conn:
        for name in (TEST_DB_NAME, RC_DB_NAME):
            exists = await conn.scalar(sa.text("select 1 from pg_database where datname=:n"), {"n": name})
            if not exists:
                await conn.execute(sa.text(f'create database "{name}"'))
    await admin.dispose()
    # Both, up front: the process context's sessions point at the rc database, so a plain test that
    # reaches it before any real_commit test has run on this worker still finds a current schema.
    await _reset_schema(TEST_URL)
    await _reset_schema(RC_URL)


@pytest.fixture(scope="session")
def _test_databases():
    """Create this worker's databases and build both schemas once; after that tests roll back or truncate."""
    # Its own short-lived loop: pytest-asyncio gives each test a fresh one, and nothing here outlives it.
    asyncio.run(_create_databases())


class ConcurrentSavepointSessions(AssertionError):
    pass


def _guard_savepoint_stack(sync_engine, violations):
    """Fail loudly when sessions sharing the savepoint-mode connection overlap out of order.

    Each session's transaction is a SAVEPOINT on one connection, which only works if they end in
    LIFO order: a session opened and finished inside another one is fine, but when an outer one
    ends while an inner one is still open, Postgres silently discards the inner savepoint and that
    session later fails with InvalidSavepointSpecificationError, which app code catching Exception
    tends to swallow. Catch the out-of-order end itself instead, and record it so the test fails at
    teardown even if the raise is swallowed too.
    """
    stack: list[str] = []

    def check(name):
        if stack and stack[-1] == name:
            stack.pop()
            return
        msg = (
            f"concurrent sessions in savepoint mode: savepoint {name!r} ended while {stack[-1]!r} (another "
            "session's transaction) was still open on the shared test connection; mark the test "
            "@pytest.mark.real_commit"
            if name in stack
            else f"savepoint {name!r} ended but was never opened on the shared test connection"
        )
        violations.append(msg)
        raise ConcurrentSavepointSessions(msg)

    seq = 0

    @sa.event.listens_for(sync_engine, "savepoint")
    def _push(conn, name):
        # The event fires before the Connection names the savepoint; mirror its per-connection
        # sa_savepoint_<n> sequence (one connection per test here).
        nonlocal seq
        if name is None:
            seq += 1
            name = f"sa_savepoint_{seq}"
        stack.append(name)

    @sa.event.listens_for(sync_engine, "release_savepoint")
    def _release(conn, name, context):
        check(name)

    @sa.event.listens_for(sync_engine, "rollback_savepoint")
    def _rollback(conn, name, context):
        check(name)


def _route_app_sessions(monkeypatch, m):
    # App code outside a request (task runtime, CLI, renderer, scheduler) opens the process context's
    # sessions itself (#53); both contexts, so jobfinder.context's copy agrees.
    monkeypatch.setattr(core_context.default_context(), "sessions", m)
    monkeypatch.setattr(default_context(), "sessions", m)


@pytest_asyncio.fixture
async def maker(request, monkeypatch, _test_databases):
    """The one session factory for a test, its HTTP client and the process context's sessions.

    Every session joins a single outer transaction on one connection, so the app's commit() only
    releases a SAVEPOINT and the whole test is rolled back at the end. Sessions that run
    concurrently (asyncio.gather, background loops) can't share one connection's SAVEPOINT stack,
    so tests doing that, or needing commits visible on other connections, are marked real_commit
    and get a real engine on their own database, truncated per test, instead.

    Two differences from production to keep in mind: Postgres now() (server defaults like
    created_at) is fixed for the whole test, and a session opened inside another one's savepoint
    loses its "committed" work if the outer session rolls back. Sessions that overlap out of order
    on the shared connection fail the test with ConcurrentSavepointSessions.
    """
    if request.node.get_closest_marker("real_commit"):
        await _truncate_all(RC_URL)
        eng = create_async_engine(RC_URL, poolclass=NullPool)
        m = async_sessionmaker(eng, expire_on_commit=False)
        _route_app_sessions(monkeypatch, m)
        yield m
        await eng.dispose()
        return

    eng = create_async_engine(TEST_URL, poolclass=NullPool)
    violations: list[str] = []
    _guard_savepoint_stack(eng.sync_engine, violations)
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
    if violations:
        pytest.fail(violations[0], pytrace=False)


@pytest_asyncio.fixture
async def db(maker):
    async with maker() as s:
        yield s


@pytest_asyncio.fixture
async def client(maker, isolated_store):
    """The real app (create_app) on a context whose sessions are the test's, and the test's store."""
    bind = maker.kw["bind"]  # the savepoint-mode connection, or the real_commit engine
    ctx = build_context(get_settings(), engine=getattr(bind, "engine", bind), sessions=maker, store=isolated_store)
    app = create_app(ctx=ctx)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await ctx.http.aclose()  # the maker fixture owns the engine


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Never let a test touch a real document store: the process's (`default_context().store`) is the test's."""
    store = DocumentStore(tmp_path / "store")
    monkeypatch.setattr(get_settings(), "store_dir", str(store.root))
    monkeypatch.setattr(default_context(), "store", store)
    return store


@pytest_asyncio.fixture(autouse=True)
async def _close_shared_http():
    """worker-io's shared board-fetch transport (tasks.io.shared_http) belongs to one test's event loop."""
    yield
    io = sys.modules.get("jobfinder.tasks.io")
    if io is not None:
        await io.close_http()


@pytest.fixture(autouse=True)
def local_claude_gate(monkeypatch):
    """Claude permits in memory, so a test's Claude calls never need valkey (test_claude_gate.py does)."""
    gate = LocalGate()
    monkeypatch.setattr(claude_gate, "default_gate", lambda: gate)
    return gate


@pytest.fixture(autouse=True)
def isolated_claude_auth(tmp_path, monkeypatch):
    """Never let a test read or write a real credentials directory."""
    s = get_settings()
    monkeypatch.setattr(s, "claude_auth_dir", str(tmp_path / "claude-auth"))
    monkeypatch.setattr(s, "claude_code_oauth_token", "")


@pytest.fixture
def web():
    """The network, mocked with respx for this test; see the helpers in tests/fakes.py."""
    with MockWeb() as router:
        yield router
    assert not router.stray, f"no route matched: {[(r.headers['host'], str(r.url)) for r in router.stray]}"


@pytest.fixture
def creds():
    return {"email": "sam@example.com", "password": "correct-horse-battery"}


@pytest_asyncio.fixture
async def make_account(client, maker):
    """Insert an active (activated) user + profile directly, then optionally log in through the real endpoint."""
    from jobfinder.models import Profile, User
    from jobfinder.services.auth import hash_password

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

    monkeypatch.setattr("jobfinder.integrations.smtp.send_email", fake)
    return sent


@pytest_asyncio.fixture
async def authed(client, creds, make_account):
    await make_account(creds["email"], creds["password"])
    return client


class Published(list):
    """What the ledger published after commit: [(queue, kind, task_id)]. Await `settle()` first: the
    publish runs as a task scheduled by the commit. `marked` holds the ids it then marked sent, in
    savepoint mode only (there the mark can't open its own session on the shared connection)."""

    def __init__(self):
        super().__init__()
        self.marked: list[int] = []

    async def settle(self) -> "Published":
        from jobfinder.tasks import ledger

        while ledger._publishing:
            await asyncio.gather(*list(ledger._publishing))
        return self


_PUBLISH = ledger._publish  # as the app has it, for `published` to put back


@pytest.fixture(autouse=True)
def no_publish(monkeypatch):
    """No test reaches valkey: a commit publishes nothing unless the test asks for `published` (#53)."""
    async def nothing(pending):
        pass

    monkeypatch.setattr(ledger, "_publish", nothing)


@pytest.fixture
def published(request, monkeypatch, no_publish):
    """The after-commit publish, with valkey replaced by a recorder (tasks.brokers.publish). real_commit
    tests mark the published rows sent for real; savepoint-mode ones record the ids in `marked` instead."""
    from jobfinder.tasks import brokers, ledger

    out = Published()
    monkeypatch.setattr(ledger, "_publish", _PUBLISH)

    async def record(queue, kind, task_id):
        out.append((queue, kind, task_id))

    async def record_marks(ids):
        out.marked.extend(ids)

    monkeypatch.setattr(brokers, "publish", record)
    if not request.node.get_closest_marker("real_commit"):
        monkeypatch.setattr(ledger, "_mark_published", record_marks)
    return out


# ── no real network (#53) ────────────────────────────────────────────────
# A test never reaches past this machine: connecting a socket anywhere but loopback or a unix socket, or
# resolving a name other than localhost, raises at once (and fails the test even if the code under test
# swallows the error), so an HTTP call that respx or MockWeb doesn't mock fails loudly instead of fetching.
# respx intercepts httpx before any socket is opened. Postgres and valkey are on localhost, or on the hosts
# TEST_ADMIN_URL, VALKEY_URL and TEST_VALKEY_URL name, which are let through too.
_blocked: list[str] = []


class RealNetworkCall(RuntimeError):
    pass


def _loopback(host) -> bool:
    host = host.decode() if isinstance(host, bytes) else host
    if host is None or host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _ip_literal(host) -> bool:
    try:
        ipaddress.ip_address((host.decode() if isinstance(host, bytes) else str(host)).split("%")[0])
        return True
    except ValueError:
        return False


def _service_hosts(urls) -> set[str]:
    """The hosts of the test's own services (Postgres from TEST_ADMIN_URL, valkey from VALKEY_URL and
    TEST_VALKEY_URL) that aren't loopback: CI or a developer may point them at another machine."""
    return {h for h in (urlsplit(u).hostname for u in urls if u) if h and not _loopback(h)}


def _addresses(hosts: set[str], getaddrinfo) -> set[str]:
    """`hosts` and the addresses they resolve to, which is what connect() is then called with."""
    out = set(hosts)
    for host in hosts:
        try:
            out |= {info[4][0] for info in getaddrinfo(host, None)}
        except OSError:
            pass  # unresolvable now: connecting to it fails on its own
    return out


def _permitted(host, allowed: set[str]) -> bool:
    if _loopback(host):
        return True
    host = host.decode() if isinstance(host, bytes) else str(host)
    return host in allowed or host.split("%")[0] in allowed


def _refuse(what: str):
    _blocked.append(what)
    raise RealNetworkCall(f"tests must not reach the network ({what}): mock it with respx or tests.fakes.MockWeb")


@pytest.fixture(scope="session", autouse=True)
def _deny_network():
    real_connect, real_connect_ex, real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
    allowed = _addresses(_service_hosts([ADMIN_URL, get_settings().valkey_url, TEST_VALKEY_URL]), real_getaddrinfo)

    def check(sock, address):
        if sock.family != getattr(socket, "AF_UNIX", None) and not (
            isinstance(address, tuple) and _permitted(address[0], allowed)
        ):
            _refuse(f"connect to {address!r}")

    def connect(self, address):
        check(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        check(self, address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if not (_permitted(host, allowed) or _ip_literal(host)):  # an IP literal needs no lookup; connect() checks it
            _refuse(f"DNS lookup of {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    mp = pytest.MonkeyPatch()
    mp.setattr(socket.socket, "connect", connect)
    mp.setattr(socket.socket, "connect_ex", connect_ex)
    mp.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _no_network_call_was_swallowed():
    _blocked.clear()
    yield
    assert not _blocked, f"the test tried to reach the network: {_blocked}"
