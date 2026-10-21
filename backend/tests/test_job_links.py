from urllib.parse import quote

import httpx
import pytest
import sqlalchemy as sa
from security_payloads import SQLI

from jobfinder.core.config import get_settings
from jobfinder.models import Company, Job, JobLink, Task
from jobfinder.services import resolver
from jobfinder.services.board_selection import select_boards
from jobfinder.services.resolver import BadLink, clean_url, name_from_slug, resolve_link
from jobfinder.services.resolver.rungs.url_pattern import UrlPatternRung
from jobfinder.tasks import io
from tests.fakes import serve

GH = "https://boards.greenhouse.io/acme/jobs/123"
GOOD = {"jobs": [
    {"id": 123, "title": "Robotics Engineer", "absolute_url": GH, "location": {"name": "Boston, MA"}},
    {"id": 124, "title": "Controls Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/124",
     "location": {"name": "Remote"}},
]}


def board_json(web, json, status=200):
    serve(web, status=status, json=json)
    return httpx.AsyncClient()


async def tasks(db, kind):
    return (await db.scalars(sa.select(Task).where(Task.kind == kind))).all()


# ── input hygiene ─────────────────────────────────────────────────────────

def test_clean_url_normalises():
    assert clean_url("  HTTPS://Boards.Greenhouse.IO/acme/jobs/1#apply ") == "https://boards.greenhouse.io/acme/jobs/1"
    assert clean_url("https://boards.greenhouse.io/embed/job_app?for=acme&token=1").endswith("?for=acme&token=1")


@pytest.mark.parametrize("bad", [
    "", "   ", "javascript:alert(1)", "data:text/html,<script>x</script>", "ftp://x.com/jobs", "file:///etc/passwd",
    "//boards.greenhouse.io/acme", "boards.greenhouse.io/acme", "https://user:pw@boards.greenhouse.io/acme",
    "https://boards.greenhouse.io/acme\n/jobs", "https://boards.greenhouse.io/a b", "https://" + "a" * 2050 + ".com",
    "https://[::1", "http://",
])
def test_clean_url_rejects(bad):
    with pytest.raises(BadLink):
        clean_url(bad)


def test_name_from_slug():
    assert name_from_slug("greenhouse", "acme-robotics") == "Acme Robotics"
    assert name_from_slug("workday", "boeing.wd1.EXTERNAL_CAREERS") == "Boeing"
    assert name_from_slug("lever", "NASA_jpl") == "NASA Jpl"


# ── API ───────────────────────────────────────────────────────────────────

async def test_unknown_link_goes_through_the_ladder(authed, maker, monkeypatch, web):
    r = await authed.post("/api/job-links", json={"url": "https://careers.example.com/jobs/42"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "resolving" and r.json()["company"] is None
    monkeypatch.setattr(resolver, "RUNGS", [UrlPatternRung()])  # only rung 1: nothing can read this site
    async with maker() as db:
        [task] = await tasks(db, "resolve_job_link")
        out = await resolve_link(db, task.payload["link_id"], client=board_json(web, {}))
        await db.commit()  # what the resolve_job_link task commits
    assert out["status"] == "unresolved"
    body = (await authed.get(f"/api/job-links/{r.json()['id']}")).json()
    assert body["status"] == "unresolved" and "can't read" in body["error"]


async def test_known_board_resolves_immediately(authed, maker):
    async with maker() as db:
        c = Company(name="Acme", ats="greenhouse", slug="acme", origin="seed")
        db.add(c)
        await db.flush()
        db.add_all([
            Job(source="greenhouse", external_id="123", company_id=c.id, company_name="Acme", title="Robotics Engineer",
                url=GH, dedupe_hash="h1"),
            Job(source="greenhouse", external_id="9", company_id=c.id, company_name="Acme", title="Old", url="https://x/9",
                dedupe_hash="h2", is_active=False),
        ])
        await db.commit()
    r = await authed.post("/api/job-links", json={"url": GH})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "resolved" and body["company"] == "Acme" and body["job_count"] == 1
    assert body["pasted_job_id"] is not None and body["rung"] == "url_pattern"
    detail = (await authed.get(f"/api/job-links/{body['id']}")).json()
    assert [j["title"] for j in detail["jobs"]] == ["Robotics Engineer"]
    async with maker() as db:
        assert await tasks(db, "resolve_job_link") == []


async def test_new_board_is_queued_resolved_and_shared(authed, maker, web):
    r = await authed.post("/api/job-links", json={"url": GH + "#top"})
    assert r.status_code == 202, r.text
    link_id = r.json()["id"]
    assert r.json()["status"] == "resolving" and r.json()["url"] == GH

    async with maker() as db:
        [task] = await tasks(db, "resolve_job_link")
        assert task.payload == {"link_id": link_id}
        async with board_json(web, GOOD) as c:
            out = await resolve_link(db, link_id, client=c)
            await db.commit()  # what the resolve_job_link task commits
    assert out["status"] == "resolved" and out["added"] and out["new"] == 2 and out["rung"] == "url_pattern"

    body = (await authed.get(f"/api/job-links/{link_id}")).json()
    assert body["status"] == "resolved" and body["company"] == "Acme" and body["job_count"] == 2
    assert {j["title"] for j in body["jobs"]} == {"Robotics Engineer", "Controls Engineer"}
    async with maker() as db:
        company = await db.scalar(sa.select(Company).where(Company.slug == "acme"))
        assert company.origin == "user" and company.enabled and company.validated_at
        # shared: user boards are always fetched, like seeds, even past the discovered-board cap
        assert select_boards([company], [], cap=1) == [company]


async def test_duplicate_paste_returns_same_link(authed):
    first = await authed.post("/api/job-links", json={"url": "https://careers.example.com/x"})
    again = await authed.post("/api/job-links", json={"url": "https://careers.example.com/x#frag"})
    assert first.status_code == 202
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert len((await authed.get("/api/job-links")).json()) == 1


async def test_daily_limit(authed, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_links_per_user_day", 2)
    for i in range(2):
        assert (await authed.post("/api/job-links", json={"url": f"https://example.com/{i}"})).status_code == 202
    r = await authed.post("/api/job-links", json={"url": "https://example.com/3"})
    assert r.status_code == 429 and "2 job links a day" in r.json()["detail"]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://u:p@boards.greenhouse.io/acme", "not a url"])
async def test_api_rejects_bad_urls(authed, url):
    r = await authed.post("/api/job-links", json={"url": url})
    assert r.status_code == 422


@pytest.mark.parametrize("payload", SQLI)
async def test_sql_injection_payloads_are_inert(authed, maker, payload):
    url = f"https://boards.greenhouse.io/{quote(payload, safe='')}/jobs/1"
    r = await authed.post("/api/job-links", json={"url": url})
    assert r.status_code == 202 and r.json()["company"] is None  # SLUG_RE refuses it as a board
    assert r.json()["url"] == url  # stored verbatim as data
    async with maker() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Company)) == 0  # table still there, untouched
        assert await db.scalar(sa.select(sa.func.count()).select_from(JobLink)) == 1


async def test_links_are_private(authed, client, make_account):
    link_id = (await authed.post("/api/job-links", json={"url": "https://example.com/mine"})).json()["id"]
    await make_account("other@example.com", "another-good-password")  # the client now carries this user's cookie
    assert (await client.get(f"/api/job-links/{link_id}")).status_code == 404
    assert (await client.delete(f"/api/job-links/{link_id}")).status_code == 404
    assert (await client.get("/api/job-links")).json() == []


async def test_delete_keeps_the_shared_board(authed, maker):
    async with maker() as db:
        db.add(Company(name="Acme", ats="greenhouse", slug="acme", origin="user"))
        await db.commit()
    link_id = (await authed.post("/api/job-links", json={"url": GH})).json()["id"]
    assert (await authed.delete(f"/api/job-links/{link_id}")).status_code == 204
    assert (await authed.get("/api/job-links")).json() == []
    async with maker() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Company)) == 1


async def test_disabled_by_setting(authed, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_links_enabled", False)
    assert (await authed.post("/api/job-links", json={"url": GH})).status_code == 404
    assert (await authed.get("/api/job-links")).status_code == 404


async def test_requires_login(client):
    assert (await client.post("/api/job-links", json={"url": GH})).status_code == 401


# ── resolver ──────────────────────────────────────────────────────────────

async def _link(db, url=GH, user_id=None):
    from factories import make_user

    uid = user_id or (await make_user(db)).id
    link = JobLink(user_id=uid, url=url, status="resolving")
    db.add(link)
    await db.commit()
    return link


async def test_resolve_records_unreadable_board(maker, web):
    async with maker() as db:
        link = await _link(db)
        async with board_json(web, {"error": "nope"}, status=404) as c:
            out = await resolve_link(db, link.id, client=c)
            await db.commit()  # what the resolve_job_link task commits
        await db.refresh(link)
    assert out["status"] == "unresolved" and link.status == "unresolved" and "404" in link.error
    assert link.resolved_at is not None and link.company_id is None


async def test_resolve_never_leaves_link_stuck(maker, monkeypatch, web):
    async def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(resolver, "add_board", boom)
    async with maker() as db:
        link = await _link(db)
        out = await resolve_link(db, link.id, client=board_json(web, GOOD))
        await db.commit()  # what the resolve_job_link task commits
        await db.refresh(link)
    assert out == {"error": "RuntimeError"} and link.status == "unresolved" and "went wrong" in link.error


async def test_resolve_deleted_link(maker):
    async with maker() as db:
        assert await resolve_link(db, 999_999) == {"skipped": "link deleted"}


async def test_handler_queues_matching_when_new_jobs(maker, monkeypatch):
    async def fake(db, link_id, **kw):
        return {"status": "resolved", "new": 3}

    monkeypatch.setattr(resolver, "resolve_link", fake)
    async with maker() as db:
        task = Task(kind="resolve_job_link", payload={"link_id": 1})
        assert (await io.resolve_job_link(db, None, task, io.ResolveLinkArgs(link_id=1)))["new"] == 3
        assert len(await tasks(db, "plan_work")) == 1
