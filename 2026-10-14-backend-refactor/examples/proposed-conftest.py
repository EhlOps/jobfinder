# Sketch: backend/tests/conftest.py after D4 + D7. No env juggling before imports, no DDL per test.
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from jobfinder.app import create_app
from jobfinder.core.config import Settings
from jobfinder.core.context import build_context
from tests.fakes import FakeAI, FakeClock


@pytest.fixture(scope="session")
def settings(worker_id) -> Settings:
    # pytest-xdist gives each worker its own database: jobfinder_test_gw0, _gw1, ...
    return Settings(db={"url": test_db_url(f"jobfinder_test_{worker_id}")},
                    auth={"secret_key": "test-secret-key-0123456789-abcdefghijklmnop"},
                    public_base_url="http://test")


@pytest_asyncio.fixture(scope="session")
async def engine(settings):
    eng = await create_fresh_database(settings)   # drop/create schema + create_all ONCE per session
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    """Each test runs inside one outer transaction that is rolled back at the end.
    Code under test may still call session.begin()/commit(): those become SAVEPOINTs."""
    async with engine.connect() as conn:
        outer = await conn.begin()
        maker = async_sessionmaker(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        yield maker
        await outer.rollback()


@pytest_asyncio.fixture
async def ctx(settings, engine, db, tmp_path):
    return build_context(settings, engine=engine, sessions=db, ai=FakeAI(), clock=FakeClock(),
                         store_dir=tmp_path / "store")


@pytest_asyncio.fixture
async def client(ctx):
    app = create_app(ctx=ctx)  # no dependency_overrides, no monkeypatching db.SessionLocal
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
