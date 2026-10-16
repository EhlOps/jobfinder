import sqlalchemy as sa

from jobfinder.api.health import alembic_heads


async def test_healthz(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}


def test_alembic_heads_reads_the_shipped_scripts(tmp_path):
    heads = alembic_heads()
    assert len(heads) == 1  # one linear history; a branch would need a merge revision
    assert alembic_heads(tmp_path) == frozenset()  # an image without alembic.ini


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
    (head,) = alembic_heads()
    await _stamp(db, head)
    r = await client.get("/readyz")
    assert (r.status_code, r.json()) == (200, {"status": "ok", "revision": head})


async def test_readyz_503_without_alembic_version(client):
    r = await client.get("/readyz")
    assert r.status_code == 503 and "not migrated" in r.json()["reason"]


async def test_readyz_503_when_the_schema_is_behind(client, db):
    (head,) = alembic_heads()
    await _stamp(db, "0001")
    r = await client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["reason"] == f"schema at 0001, image expects {head}"


async def test_readyz_503_when_the_database_is_down(client, monkeypatch):
    def broken():
        raise ConnectionRefusedError("no db")

    monkeypatch.setattr(_app(client).state.ctx, "sessions", broken)
    r = await client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["reason"] == "database unreachable: ConnectionRefusedError"


async def test_readyz_503_without_alembic_scripts(client, monkeypatch):
    monkeypatch.setattr(_app(client).state, "alembic_heads", frozenset())
    r = await client.get("/readyz")
    assert r.status_code == 503 and r.json()["reason"] == "no alembic scripts in this image"
