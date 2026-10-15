from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from security_payloads import SCRIPT_URLS, SQLI

from jobfinder.models import Company, CovetedTarget, Job, Task

BASE = "/api/admin/coverage"


async def _seed(maker):
    now = datetime.now(UTC)
    async with maker() as db:
        broken = Company(name="BrokenCo", ats="greenhouse", slug="broken", enabled=False, coveted_tier=1,
                         coveted_score=80, disabled_reason="5 consecutive failures")
        fine = Company(name="FineCo", ats="lever", slug="fine", coveted_tier=1, coveted_score=90,
                       last_success_at=now)
        mid = Company(name="MidCo", ats="ashby", slug="mid", coveted_tier=2, coveted_score=60,
                      consecutive_failures=2, last_success_at=now - timedelta(days=20))
        dull = Company(name="DullCo", ats="lever", slug="dull", coveted_tier=3, coveted_score=10)
        db.add_all([broken, fine, mid, dull])
        await db.flush()
        db.add(Job(source="lever", external_id="1", company_id=fine.id, company_name="FineCo", title="Eng",
                   url="http://x", dedupe_hash="h1"))
        db.add(CovetedTarget(name="Broken", domain="broken.com", source="audit", company_id=broken.id,
                             last_error="https://broken.com/careers: HTTP 404"))
        db.add(CovetedTarget(name="Wanted", domain="wanted.com", source="admin", score=100))
        await db.commit()
        return broken.id, fine.id, mid.id, dull.id


async def _tasks(maker, kind):
    async with maker() as db:
        return (await db.scalars(sa.select(Task).where(Task.kind == kind))).all()


async def test_requires_admin(client, make_account):
    endpoints = [("get", BASE), ("post", f"{BASE}/pin"), ("post", f"{BASE}/unpin"), ("post", f"{BASE}/rescore"),
                 ("post", f"{BASE}/1/resolve"), ("post", f"{BASE}/targets/1/resolve")]
    for method, url in endpoints:
        kw = {"json": {"company_id": 1}} if method == "post" else {}
        assert (await getattr(client, method)(url, **kw)).status_code == 401
    await make_account("user@x.com")
    for method, url in endpoints:
        kw = {"json": {"company_id": 1}} if method == "post" else {}
        assert (await getattr(client, method)(url, **kw)).status_code == 403


async def test_rows_sorted_with_status_and_targets(client, maker, make_account):
    broken, fine, mid, _ = await _seed(maker)
    await make_account("owner@x.com", admin=True)
    body = (await client.get(BASE)).json()
    rows = body["companies"]
    assert [r["name"] for r in rows] == ["FineCo", "BrokenCo", "MidCo"]  # tier, then score; tier 3 left out
    by = {r["company_id"]: r for r in rows}
    assert by[fine]["board_status"] == "ok" and by[fine]["job_count"] == 1 and not by[fine]["gap"]
    assert by[broken]["board_status"] == "disabled" and by[broken]["gap"]
    assert by[broken]["last_error"] == "5 consecutive failures" and "HTTP 404" in by[broken]["resolve_error"]
    assert by[broken]["careers_url"] == "https://broken.com/careers"
    assert by[mid]["board_status"] == "failing" and by[mid]["gap"]
    assert [t["domain"] for t in body["targets"]] == ["wanted.com"]
    assert body["targets"][0]["careers_url"] == "https://wanted.com/careers"


async def test_pin_and_unpin_company(client, maker, make_account):
    *_, dull = await _seed(maker)
    await make_account("owner@x.com", admin=True)
    assert (await client.post(f"{BASE}/pin", json={"company_id": dull})).status_code == 200
    row = {r["company_id"]: r for r in (await client.get(BASE)).json()["companies"]}[dull]
    assert row["pinned"] is True and row["tier"] == 1

    assert (await client.post(f"{BASE}/unpin", json={"company_id": dull, "exclude": True})).status_code == 200
    row = {r["company_id"]: r for r in (await client.get(BASE)).json()["companies"]}[dull]
    assert row["pinned"] is False and row["tier"] == 3 and row["score"] == 0  # still listed so it can be undone
    assert len(await _tasks(maker, "score_coveted")) == 1

    assert (await client.post(f"{BASE}/unpin", json={"company_id": dull})).status_code == 200
    assert dull not in {r["company_id"] for r in (await client.get(BASE)).json()["companies"]}
    assert (await client.post(f"{BASE}/pin", json={"company_id": 99999})).status_code == 404
    assert (await client.post(f"{BASE}/unpin", json={})).status_code == 422


async def test_add_employer_creates_target_and_queues_resolve(client, maker, make_account):
    await make_account("owner@x.com", admin=True)
    r = await client.post(f"{BASE}/pin", json={"name": "Acme Robotics", "domain": " WWW.Acme-Robotics.com "})
    assert r.status_code == 200
    out = r.json()
    async with maker() as db:
        t = await db.get(CovetedTarget, out["target_id"])
    assert t.domain == "acme-robotics.com" and t.source == "admin" and t.company_id is None
    tasks = await _tasks(maker, "audit_coverage")
    assert len(tasks) == 1 and tasks[0].payload == {"target_id": t.id} and out["task_id"] == tasks[0].id
    # pinning the same domain again reuses the row
    again = (await client.post(f"{BASE}/pin", json={"name": "Acme", "domain": "acme-robotics.com"})).json()
    assert again["target_id"] == t.id

    assert (await client.post(f"{BASE}/unpin", json={"target_id": t.id})).status_code == 200
    assert (await client.get(BASE)).json()["targets"] == []
    assert (await client.post(f"{BASE}/unpin", json={"target_id": t.id})).status_code == 404


@pytest.mark.parametrize("domain", [
    "https://acme.com", "acme.com/careers", "acme.com:443", "acme", "a" * 256, "", *SCRIPT_URLS, *SQLI,
])
async def test_add_employer_rejects_bad_domains(client, maker, make_account, domain):
    await make_account("owner@x.com", admin=True)
    r = await client.post(f"{BASE}/pin", json={"name": "Acme", "domain": domain})
    assert r.status_code == 422
    async with maker() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(CovetedTarget)) == 0


async def test_add_employer_needs_a_name(client, make_account):
    await make_account("owner@x.com", admin=True)
    assert (await client.post(f"{BASE}/pin", json={"name": "  ", "domain": "acme.com"})).status_code == 422


@pytest.mark.parametrize("name", SQLI)
async def test_sqli_in_names_is_inert(client, maker, make_account, name):
    await make_account("owner@x.com", admin=True)
    r = await client.post(f"{BASE}/pin", json={"name": name, "domain": "acme.com"})
    assert r.status_code == 200
    targets = (await client.get(BASE)).json()["targets"]
    assert [t["name"] for t in targets] == [name]
    async with maker() as db:  # the tables are all still there
        assert await db.scalar(sa.select(sa.func.count()).select_from(Company)) == 0


async def test_resolve_and_rescore_enqueue(client, maker, make_account):
    broken, *_ = await _seed(maker)
    await make_account("owner@x.com", admin=True)
    r = await client.post(f"{BASE}/{broken}/resolve")
    assert r.status_code == 202
    again = await client.post(f"{BASE}/{broken}/resolve")
    assert again.json()["task_id"] == r.json()["task_id"]  # deduped while queued
    async with maker() as db:
        target = await db.scalar(sa.select(CovetedTarget).where(CovetedTarget.domain == "wanted.com"))
    assert (await client.post(f"{BASE}/targets/{target.id}/resolve")).status_code == 202
    assert (await client.post(f"{BASE}/99999/resolve")).status_code == 404
    assert (await client.post(f"{BASE}/targets/99999/resolve")).status_code == 404
    payloads = sorted(str(t.payload) for t in await _tasks(maker, "audit_coverage"))
    assert payloads == sorted([str({"company_id": broken}), str({"target_id": target.id})])
    assert (await client.post(f"{BASE}/rescore")).status_code == 202
    assert len(await _tasks(maker, "score_coveted")) == 1
