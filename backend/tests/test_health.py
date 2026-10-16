import asyncio

import sqlalchemy as sa

from jobfinder.api import health
from jobfinder.api.health import Revisions, alembic_revisions


async def test_healthz(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}


def _head() -> str:
    (head,) = alembic_revisions().heads  # one linear history; a branch would need a merge revision
    return head


def test_alembic_revisions_reads_the_shipped_scripts(tmp_path):
    revs = alembic_revisions()
    assert revs.heads <= revs.known and "0001" in revs.known and len(revs.known) > 1
    assert alembic_revisions(tmp_path) == Revisions(frozenset(), frozenset())  # no alembic.ini


async def _stamp(db, *revisions):
    # The test schema is built with create_all, so there is no alembic_version until a test makes one.
    await db.execute(sa.text(
        "create table if not exists alembic_version (version_num varchar(32) primary key)"
    ))
    await db.execute(sa.text("delete from alembic_version"))
    for rev in revisions:
        await db.execute(sa.text("insert into alembic_version values (:r)"), {"r": rev})
    await db.commit()


def _app(client):
    return client._transport.app


async def test_readyz_ok_at_head(client, db):
    await _stamp(db, _head())
    r = await client.get("/readyz")
    assert (r.status_code, r.json()) == (200, {"status": "ok", "revision": _head()})


async def test_readyz_503_without_alembic_version(client):
    r = await client.get("/readyz")
    assert r.status_code == 503 and "not migrated" in r.json()["reason"]


async def test_readyz_503_when_the_schema_is_behind(client, db):
    await _stamp(db, "0001")
    r = await client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["reason"] == f"schema at 0001, image expects {_head()}"


async def test_readyz_503_with_an_empty_alembic_version(client, db):
    await _stamp(db)
    r = await client.get("/readyz")
    assert r.status_code == 503 and r.json()["reason"].startswith("schema at nothing")


async def test_readyz_stays_ready_when_a_newer_image_migrated(client, db):
    # The old pods keep serving while the new image rolls out after the migrate Job.
    await _stamp(db, "9999_from_a_newer_image")
    r = await client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok", "revision": "9999_from_a_newer_image", "note": f"schema newer than {_head()}",
    }


async def test_readyz_503_when_the_database_is_down(client, monkeypatch):
    def broken():
        raise ConnectionRefusedError("no db")

    monkeypatch.setattr(_app(client).state.ctx, "sessions", broken)
    r = await client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["reason"] == "database unreachable: ConnectionRefusedError"


async def test_readyz_503_when_the_database_hangs(client, monkeypatch):
    async def hang(request):
        await asyncio.sleep(60)

    monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(health, "_db_revisions", hang)
    r = await client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["reason"] == "database unreachable: TimeoutError"


async def test_readyz_503_without_alembic_scripts(client, monkeypatch):
    monkeypatch.setattr(_app(client).state, "alembic", Revisions(frozenset(), frozenset()))
    r = await client.get("/readyz")
    assert r.status_code == 503 and r.json()["reason"] == "no alembic scripts in this image"
