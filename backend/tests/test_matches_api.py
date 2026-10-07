from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.models import Job, JobMatch, Task

N = 0


async def add_match(maker, user_id, title, score, *, status="new", active=True, workplace=None, company="Acme", version=1, posted_days_ago=1):
    global N
    N += 1
    async with maker() as db:
        j = Job(
            source="greenhouse", external_id=f"m{N}", company_name=company, title=title, url=f"https://x/{N}",
            location="Boston, MA", workplace_type=workplace, is_active=active, description_text=f"About {title}",
            dedupe_hash=f"h{N}", posted_at=datetime.now(UTC) - timedelta(days=posted_days_ago),
        )
        db.add(j)
        await db.flush()
        m = JobMatch(user_id=user_id, job_id=j.id, profile_version=version, prefilter_score=50, llm_score=score,
                     confidence=0.8, verdict="good", reasons=["r1"], gaps=["g1"], unknowns=[{"question": "q?", "why": "w"}], status=status)
        db.add(m)
        await db.commit()
        return m.id


async def me(client):
    return (await client.get("/api/auth/me")).json()["id"]


async def test_list_filters_sort_counts(authed, engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    uid = await me(authed)
    await authed.put("/api/profile/status", json={"target_roles": ["x"]})   # profile version becomes 2
    await add_match(maker, uid, "Backend Engineer", 90, workplace="remote", company="Stripe", posted_days_ago=10, version=2)
    await add_match(maker, uid, "Frontend Engineer", 70, workplace="hybrid", version=2, posted_days_ago=1)
    await add_match(maker, uid, "Data Engineer", 40, version=2)
    await add_match(maker, uid, "SRE", 85, status="saved", version=1)
    await add_match(maker, uid, "Dismissed Role", 95, status="dismissed")
    await add_match(maker, uid, "Closed Role", 99, active=False)
    await add_match(maker, uid, "Closed But Saved", 60, status="saved", active=False)

    async def listing(**params):
        r = await authed.get("/api/matches", params=params)
        assert r.status_code == 200
        return r.json()

    async def titles(**params):
        return [i["job"]["title"] for i in (await listing(**params))["items"]]

    r = await listing()
    # new + saved by score; the closed job is hidden unless it was saved
    assert await titles() == ["Backend Engineer", "SRE", "Frontend Engineer", "Closed But Saved", "Data Engineer"]
    assert r["total"] == 5 and r["counts"] == {"new": 3, "saved": 2, "applied": 0, "dismissed": 1}
    stale = {i["job"]["title"]: i["stale"] for i in r["items"]}
    assert stale["SRE"] is True and stale["Backend Engineer"] is False       # SRE was scored against profile v1

    assert await titles(min_score=65) == ["Backend Engineer", "SRE", "Frontend Engineer"]
    assert await titles(workplace="remote") == ["Backend Engineer"]
    assert await titles(q="stripe") == ["Backend Engineer"]
    assert await titles(q="FRONTEND") == ["Frontend Engineer"]
    assert await titles(status="dismissed") == ["Dismissed Role"]
    assert await titles(status="saved") == ["SRE", "Closed But Saved"]
    assert (await titles(sort="recent"))[-1] == "Backend Engineer"             # posted 10 days ago: oldest
    page = await listing(limit=2, offset=2)
    assert [i["job"]["title"] for i in page["items"]] == ["Frontend Engineer", "Closed But Saved"] and page["total"] == 5
    assert (await authed.get("/api/matches", params={"status": "bogus"})).status_code == 422


async def test_detail_includes_description_and_unknowns(authed, engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    mid = await add_match(maker, await me(authed), "Backend Engineer", 88)
    d = (await authed.get(f"/api/matches/{mid}")).json()
    assert d["description"] == "About Backend Engineer" and d["unknowns"] == [{"question": "q?", "why": "w"}]
    assert d["reasons"] == ["r1"] and d["gaps"] == ["g1"] and d["job"]["url"].startswith("https://x/")


async def test_set_status_and_ownership(authed, client, engine, make_account):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    mid = await add_match(maker, await me(authed), "Backend Engineer", 88)
    r = await authed.patch(f"/api/matches/{mid}", json={"status": "saved"})
    assert r.status_code == 200 and r.json()["status"] == "saved"
    assert (await authed.patch(f"/api/matches/{mid}", json={"status": "nope"})).status_code == 422
    counts = (await authed.get("/api/matches")).json()["counts"]
    assert counts["saved"] == 1 and counts["new"] == 0

    await client.post("/api/auth/logout")
    await make_account("other@x.com", "password-1234")
    assert (await client.get(f"/api/matches/{mid}")).status_code == 404
    assert (await client.patch(f"/api/matches/{mid}", json={"status": "dismissed"})).status_code == 404
    assert (await client.get("/api/matches")).json()["items"] == []


async def test_requires_login(client):
    assert (await client.get("/api/matches")).status_code == 401
    assert (await client.post("/api/matches/refresh")).status_code == 401


async def test_refresh_requires_profile_then_enqueues_once(authed, engine):
    assert (await authed.post("/api/matches/refresh")).status_code == 422
    await authed.put("/api/profile/status", json={"target_roles": ["Backend Engineer"]})
    r = await authed.post("/api/matches/refresh")
    assert r.status_code == 202
    assert (await authed.post("/api/matches/refresh")).json()["task_id"] == r.json()["task_id"]
    async with async_sessionmaker(engine)() as db:
        t = await db.get(Task, r.json()["task_id"])
        assert t.kind == "match_user" and t.status == "queued"


async def test_non_http_job_urls_never_reach_the_client(authed, engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    uid = await me(authed)
    mids = [await add_match(maker, uid, f"Job {i}", 90 - i) for i in range(3)]
    async with maker() as db:
        for mid, url in zip(mids, ["javascript:alert(1)", "data:text/html,<script>1</script>", "https://ok.example/apply"], strict=True):
            m = await db.get(JobMatch, mid)
            (await db.get(Job, m.job_id)).url = url
        await db.commit()
    urls = {i["job"]["title"]: i["job"]["url"] for i in (await authed.get("/api/matches")).json()["items"]}
    assert urls == {"Job 0": "", "Job 1": "", "Job 2": "https://ok.example/apply"}
    assert (await authed.get(f"/api/matches/{mids[0]}")).json()["job"]["url"] == ""
