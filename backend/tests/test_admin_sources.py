from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.models import Company, Job


async def _seed(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        bad = Company(
            name="BrokenCo", ats="greenhouse", slug="broken", enabled=False, consecutive_failures=5,
            last_error="HTTP 404", disabled_reason="5 consecutive failures", last_fetched_at=datetime.now(UTC),
        )
        ok = Company(name="FineCo", ats="lever", slug="fine", last_success_at=datetime.now(UTC))
        db.add_all([bad, ok])
        await db.flush()
        db.add(Job(source="lever", external_id="1", company_id=ok.id, company_name="FineCo", title="Eng", url="http://x", dedupe_hash="h1"))
        await db.commit()
        return bad.id, ok.id


async def test_requires_admin(client, make_account):
    assert (await client.get("/api/admin/sources")).status_code == 401
    assert (await client.post("/api/admin/sources/1/enable")).status_code == 401
    await make_account("user@x.com")
    assert (await client.get("/api/admin/sources")).status_code == 403
    assert (await client.post("/api/admin/sources/1/enable")).status_code == 403


async def test_rows_reflect_failures(client, engine, make_account):
    await _seed(engine)
    await make_account("owner@x.com", admin=True)
    rows = (await client.get("/api/admin/sources")).json()
    assert [r["name"] for r in rows] == ["BrokenCo", "FineCo"]  # failing first
    bad, ok = rows
    assert bad["enabled"] is False and bad["consecutive_failures"] == 5
    assert bad["last_error"] == "HTTP 404" and bad["disabled_reason"] == "5 consecutive failures"
    assert ok["job_count"] == 1 and ok["origin"] == "seed" and ok["last_success_at"]


async def test_enable_resets_counters(client, engine, make_account):
    bad_id, _ = await _seed(engine)
    await make_account("owner@x.com", admin=True)
    r = await client.post(f"/api/admin/sources/{bad_id}/enable")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["consecutive_failures"] == 0
    assert body["disabled_reason"] is None and body["last_error"] is None
    again = {x["id"]: x for x in (await client.get("/api/admin/sources")).json()}[bad_id]
    assert again["enabled"] is True and again["consecutive_failures"] == 0
    assert (await client.post("/api/admin/sources/9999/enable")).status_code == 404
