import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.config import get_settings
from jobfinder.ingest import service
from jobfinder.models import Company
from tests.test_ingest_service import fake_fetcher, make_company, posting, status_error


@pytest.fixture
def maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def limit3(monkeypatch):
    monkeypatch.setattr(get_settings(), "ingest_max_consecutive_failures", 3)


async def test_counters_reset_on_success(maker, monkeypatch):
    async with maker() as db, httpx.AsyncClient() as client:
        c = await make_company(db)
        monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", fake_fetcher(exc=status_error(404)))
        await service.ingest_company(db, client, c)
        await service.ingest_company(db, client, c)
        assert c.consecutive_failures == 2 and c.enabled and c.last_success_at is None

        monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", fake_fetcher([posting(1)]))
        await service.ingest_company(db, client, c)
        assert c.consecutive_failures == 0 and c.last_error is None
        assert c.last_success_at is not None and c.enabled and c.disabled_reason is None


async def test_board_disabled_after_n_failures(maker, monkeypatch):
    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", fake_fetcher(exc=status_error(404)))
    async with maker() as db, httpx.AsyncClient() as client:
        c = await make_company(db)
        for _ in range(2):
            await service.ingest_company(db, client, c)
        assert c.enabled and c.disabled_reason is None
        await service.ingest_company(db, client, c)
        assert c.enabled is False and c.consecutive_failures == 3
        assert "3 consecutive failures" in c.disabled_reason and "not found" in c.disabled_reason
    async with maker() as db:
        row = await db.get(Company, c.id)
        assert row.enabled is False and row.disabled_reason


async def test_raising_fetcher_does_not_stop_the_run(maker, monkeypatch):
    async def boom(client, slug, name):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", boom)
    monkeypatch.setitem(service.ATS_FETCHERS, "lever", fake_fetcher([posting(1, source="lever")]))
    monkeypatch.setitem(service.ATS_FETCHERS, "ashby", fake_fetcher([posting(2, source="ashby")]))
    monkeypatch.setattr(service, "load_seed", lambda: [
        {"name": "A", "ats": "greenhouse", "slug": "a"},
        {"name": "B", "ats": "lever", "slug": "b"},
        {"name": "C", "ats": "ashby", "slug": "c"},
    ])
    results = {r.company: r for r in await service.ingest_boards(maker)}
    assert "kaboom" in results["A"].error
    assert results["B"].error is None and results["C"].error is None
    assert results["B"].fetched == 1 and results["C"].fetched == 1
    async with maker() as db:
        rows = {c.name: c for c in (await db.scalars(sa.select(Company))).all()}
    assert rows["A"].consecutive_failures == 1 and rows["B"].consecutive_failures == 0
    assert rows["B"].last_success_at is not None
