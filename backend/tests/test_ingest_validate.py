import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.ingest.validate import add_board, validate_board
from jobfinder.models import Company

GOOD = {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1", "location": {"name": "Remote"}}]}


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def respond(status=200, json=None, content=None):
    def handler(request):
        if content is not None:
            return httpx.Response(status, content=content)
        return httpx.Response(status, json=json)

    return handler


@pytest.fixture
def maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_valid_board():
    async with client_for(respond(json=GOOD)) as c:
        r = await validate_board("greenhouse", "acme", c)
    assert r.ok and r.job_count == 1 and r.reason == ""


async def test_empty_board():
    async with client_for(respond(json={"jobs": []})) as c:
        r = await validate_board("greenhouse", "acme", c)
    assert not r.ok and r.job_count == 0 and "no postings" in r.reason


async def test_404_board():
    async with client_for(respond(404, json={"error": "nope"})) as c:
        r = await validate_board("greenhouse", "acme", c)
    assert not r.ok and "404" in r.reason


@pytest.mark.parametrize("content", [b"<html>not json</html>", b"[1, 2]"])
async def test_malformed_board(content):
    async with client_for(respond(content=content)) as c:
        r = await validate_board("greenhouse", "acme", c)
    assert not r.ok and "malformed" in r.reason


async def test_network_error_and_bad_input():
    def boom(request):
        raise httpx.ConnectError("down")

    async with client_for(boom) as c:
        assert not (await validate_board("greenhouse", "acme", c)).ok
        assert "unknown ATS" in (await validate_board("nope", "acme", c)).reason
        assert "invalid slug" in (await validate_board("greenhouse", "../x?y", c)).reason


async def count(db):
    return await db.scalar(sa.select(sa.func.count()).select_from(Company))


async def test_add_board_inserts_valid(maker):
    async with maker() as db, client_for(respond(json=GOOD)) as c:
        company, reason = await add_board(db, "greenhouse", "acme", "Acme", client=c)
        assert company and company.origin == "discovered" and company.validated_at is not None
        assert "1 postings" in reason and await count(db) == 1


async def test_add_board_refuses_invalid(maker):
    async with maker() as db:
        for handler in (respond(404, json={}), respond(json={"jobs": []}), respond(content=b"garbage")):
            async with client_for(handler) as c:
                company, reason = await add_board(db, "greenhouse", "acme", "Acme", client=c)
            assert company is None and reason
        assert await count(db) == 0


async def test_duplicates_ignored_without_fetch(maker):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=GOOD)

    async with maker() as db, client_for(handler) as c:
        first, _ = await add_board(db, "greenhouse", "acme", "Acme", origin="seed", client=c)
        second, reason = await add_board(db, "greenhouse", "acme", "Acme Again", client=c)
        assert first and first.origin == "seed" and second is None and "duplicate" in reason
        assert len(calls) == 1 and await count(db) == 1
