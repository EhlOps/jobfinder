from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from factories import make_match, make_user

from jobfinder.ingest import coveted, http_safety
from jobfinder.ingest.coveted import (
    Signals,
    board_status,
    clean_domain,
    guess_domain,
    score,
    tier_for,
)
from jobfinder.ingest.discovery import DiscoveryResult, Source, discover_boards
from jobfinder.models import Company, CovetedTarget, Job, JobLink, Task
from jobfinder.scheduling import handlers
from tests.fakes import fake_resolve_public

GOOD = {"jobs": [
    {"id": 1, "title": "Robotics Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
     "location": {"name": "Boston, MA"}},
]}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(http_safety, "resolve_public", fake_resolve_public())


def site(routes: dict[str, httpx.Response], calls: list[str] | None = None):
    """A mock web: exact URL -> response; greenhouse's API answers GOOD for 'acme'; everything else 404."""
    def handler(request: httpx.Request):
        host = request.headers.get("host", request.url.host)  # safe_fetch connects to the pinned IP
        url = str(request.url.copy_with(host=host))
        if calls is not None:
            calls.append(url)
        if url in routes:
            return routes[url]
        if host == "boards-api.greenhouse.io":
            return httpx.Response(200, json=GOOD) if "/acme/" in url else httpx.Response(404, json={})
        if host == "boards.greenhouse.io":
            return httpx.Response(200, html="<html>Acme jobs</html>")
        return httpx.Response(404, text="not found")
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def redirect(to):
    return httpx.Response(302, headers={"Location": to})


# ---- pure scoring ----

def test_score_weights():
    assert score(Signals()) == 0
    assert score(Signals(prestige=5)) == 25
    assert score(Signals(prestige=1)) == 0
    assert score(Signals(prestige=3)) == 12.5
    assert score(Signals(listed=True)) == 25
    # engagement is log-scaled against the busiest employer (at least 10)
    assert score(Signals(engagement=10)) == 20
    assert 0 < score(Signals(engagement=1)) < score(Signals(engagement=3)) < 20
    assert score(Signals(engagement=10), engagement_max=100) < 20
    # outcomes shrink with few matches
    assert score(Signals(matches=1, mean_llm=100, applied=1)) < score(Signals(matches=50, mean_llm=100, applied=50))
    assert score(Signals(matches=1000, mean_llm=100, applied=1000)) == pytest.approx(20, abs=0.2)
    full = Signals(prestige=5, listed=True, engagement=50, matches=1000, mean_llm=100, applied=1000)
    assert 89 <= score(full, 50) <= 90


def test_tiers_and_pins():
    assert tier_for(75, None) == 1 and tier_for(74.9, None) == 2 and tier_for(50, None) == 2
    assert tier_for(49.9, None) == 3
    assert tier_for(0, True) == 1
    assert tier_for(100, False) == 3


def test_board_status():
    now = datetime.now(UTC)
    c = Company(name="A", ats="greenhouse", slug="a", enabled=True, consecutive_failures=0)
    assert board_status(None) == "none"
    assert board_status(c, now) == "ok"
    c.consecutive_failures = 2
    assert board_status(c, now) == "failing"  # never succeeded
    c.last_success_at = now - timedelta(days=3)
    assert board_status(c, now) == "ok"  # a recent success: a blip, not a gap
    c.last_success_at = now - timedelta(days=30)
    assert board_status(c, now) == "failing"
    c.consecutive_failures, c.recipe_state = 0, "degraded"
    assert board_status(c, now) == "failing"
    c.recipe_state = "disabled"
    assert board_status(c, now) == "disabled"
    c.recipe_state, c.enabled = None, False
    assert board_status(c, now) == "disabled"


@pytest.mark.parametrize("raw, want", [
    ("acme.com", "acme.com"), ("  ACME.com ", "acme.com"), ("www.acme.co.uk", "acme.co.uk"),
    ("careers.acme-robotics.io", "careers.acme-robotics.io"),
    ("https://acme.com", None), ("acme.com/careers", None), ("acme.com:8080", None), ("acme", None),
    ("ac me.com", None), ("-acme.com", None), ("acme.c0m", None), ("javascript:alert(1)", None),
    ("acme.com';DROP TABLE companies;--", None), ("a" * 250 + ".com", None), ("", None),
    ("127.0.0.1", None), ("user@acme.com", None),
])
def test_clean_domain(raw, want):
    assert clean_domain(raw) == want


def test_guess_domain():
    g = lambda **kw: guess_domain(Company(**{"name": "Acme", "ats": "greenhouse", "slug": "acme", **kw}))
    assert g(name="Jane Street", slug="janestreet") == "janestreet.com"
    assert g(name="RTX (Raytheon, Collins)", ats="workday", slug="globalhr.wd5.REC") == "rtx.com"
    assert g(ats="recipe", slug="careers.acme.io") == "acme.io"
    assert g(ats="successfactors", slug="careers.acme.de__brand") == "acme.de"
    assert g(name="!!!") is None


# ---- score_all ----

async def test_score_all_uses_every_signal(maker):
    now = datetime.now(UTC)
    async with maker() as db:
        star = Company(name="Star", ats="greenhouse", slug="Star", origin="seed", prestige_tier=5)
        plain = Company(name="Plain", ats="lever", slug="plain", origin="discovered", prestige_tier=3)
        loved = Company(name="Loved", ats="ashby", slug="loved", origin="user")
        pinned = Company(name="Pinned", ats="lever", slug="pinned", origin="discovered", coveted_pinned=True)
        hidden = Company(name="Hidden", ats="greenhouse", slug="hidden", origin="seed", prestige_tier=5,
                         coveted_pinned=False)
        db.add_all([star, plain, loved, pinned, hidden])
        await db.flush()
        # the curated discovery list (latest done run wins; an older one is ignored)
        db.add(Task(kind="discover_boards", status="done", result={"listed": ["greenhouse/old"]}))
        db.add(Task(kind="discover_boards", status="done", result={"listed": ["greenhouse/star", "greenhouse/hidden"]}))
        db.add(Task(kind="discover_boards", status="failed", result=None))
        u = await make_user(db)
        for i in range(12):  # pastes in the window count, an old one doesn't
            db.add(JobLink(user_id=u.id, url=f"https://jobs.ashbyhq.com/loved/{i}", status="resolved", company_id=loved.id))
        db.add(JobLink(user_id=u.id, url="https://old", status="resolved", company_id=plain.id,
                       created_at=now - timedelta(days=200)))
        await db.commit()
        for i in range(10):
            _, job = await make_match(db, u.id, company="Loved", score=90, status="applied" if i % 2 else "saved")
            job.company_id = loved.id
        await db.commit()

        out = await coveted.score_all(db)
        rows = {c.name: c for c in (await db.scalars(sa.select(Company))).all()}
    assert out["scored"] == 5 and out["listed"] == 2
    assert rows["Star"].coveted_score == 50 and rows["Star"].coveted_tier == 2  # prestige 25 + list 25
    assert rows["Plain"].coveted_score == 0 and rows["Plain"].coveted_tier == 3  # default prestige doesn't count
    loved_score = rows["Loved"].coveted_score
    assert 20 < loved_score < 40 and rows["Loved"].coveted_tier == 3  # engagement 20 + some outcomes
    assert rows["Pinned"].coveted_tier == 1
    assert rows["Hidden"].coveted_score == 0 and rows["Hidden"].coveted_tier == 3


async def test_discovery_records_curated_list_boards(maker):
    async def curated(_):
        return [("greenhouse", "Acme"), ("lever", "beta")]

    async def other(_):
        return [("ashby", "gamma")]

    async with maker() as db, site({}) as c:
        res: DiscoveryResult = await discover_boards(db, c, max_candidates=0, sources=[
            Source("lists", "t", True, curated, curated=True), Source("hn", "t", True, other)])
    assert res.as_dict()["listed"] == ["greenhouse/acme", "lever/beta"]


# ---- the audit ----

async def test_pinned_target_resolves_through_its_careers_page(maker):
    routes = {"https://acme.com/careers": redirect("https://boards.greenhouse.io/acme")}
    async with maker() as db, site(routes) as c:
        t = CovetedTarget(name="Acme", domain="acme.com", source="admin", score=100)
        db.add(t)
        await db.commit()
        out = await coveted.audit_coverage(db, client=c)
        await db.refresh(t)
        company = await db.get(Company, t.company_id)
        jobs = (await db.scalars(sa.select(Job).where(Job.company_id == company.id))).all()
    assert out["tried"] == 1 and out["resolved"] == 1
    assert company.ats == "greenhouse" and company.slug == "acme" and company.origin == "user"
    assert company.coveted_pinned is True and company.coveted_tier == 1  # an admin pin carries over
    assert t.last_resolved_at is not None and t.last_error is None
    assert len(jobs) == 1  # fetched right away


async def test_failed_target_records_why_and_tries_one_family(maker):
    calls: list[str] = []
    async with maker() as db, site({}, calls) as c:
        t = CovetedTarget(name="Nope", domain="nope.com", source="admin")
        db.add(t)
        await db.commit()
        out = await coveted.audit_coverage(db, client=c)
        await db.refresh(t)
    assert out["resolved"] == 0
    assert t.company_id is None and t.last_resolved_at is not None
    assert "https://nope.com/careers" in t.last_error and "https://jobs.nope.com/" in t.last_error
    pages = [u for u in calls if not u.endswith("/robots.txt")]
    assert pages == ["https://nope.com/careers", "https://careers.nope.com/", "https://jobs.nope.com/"]


async def test_audit_picks_coveted_gaps_oldest_first_and_respects_limit(maker):
    now = datetime.now(UTC)
    async with maker() as db, site({}) as c:
        broken = Company(name="Broken Co", ats="greenhouse", slug="brokenco", enabled=False, coveted_tier=1,
                         coveted_score=80)
        failing = Company(name="Failing", ats="lever", slug="failing", consecutive_failures=3, coveted_tier=2,
                          last_success_at=now - timedelta(days=40))
        blip = Company(name="Blip", ats="lever", slug="blip", consecutive_failures=1, coveted_tier=1,
                       last_success_at=now - timedelta(days=1))
        dull = Company(name="Dull", ats="lever", slug="dull", enabled=False, coveted_tier=3)
        db.add_all([broken, failing, blip, dull])
        db.add(CovetedTarget(name="Fresh", domain="fresh.com", last_resolved_at=now))
        db.add(CovetedTarget(name="Stale", domain="stale.com", last_resolved_at=now - timedelta(days=9)))
        await db.commit()
        out = await coveted.audit_coverage(db, limit=3, client=c)
        targets = {t.domain: t for t in (await db.scalars(sa.select(CovetedTarget))).all()}
    assert set(targets) == {"brokenco.com", "failing.com", "fresh.com", "stale.com"}  # blip and dull: no gap
    assert targets["brokenco.com"].company_id == broken.id and targets["brokenco.com"].source == "audit"
    tried = {r["domain"] for r in out["results"]}
    assert tried == {"brokenco.com", "failing.com", "stale.com"}  # never-tried first, then the oldest


async def test_same_failing_board_is_not_a_fix(maker):
    routes = {"https://brokenco.com/careers": redirect("https://boards.greenhouse.io/brokenco")}
    async with maker() as db, site(routes) as c:
        broken = Company(name="BrokenCo", ats="greenhouse", slug="brokenco", enabled=False, coveted_tier=1)
        db.add(broken)
        await db.commit()
        out = await coveted.audit_coverage(db, company_id=broken.id, client=c)
        t = await db.scalar(sa.select(CovetedTarget))
    assert out["resolved"] == 0 and t.company_id == broken.id
    assert "same failing board" in t.last_error


async def test_audit_survives_a_crash(maker, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(coveted, "resolve_target", boom)
    async with maker() as db, site({}) as c:
        db.add(CovetedTarget(name="X", domain="x.com"))
        await db.commit()
        out = await coveted.audit_coverage(db, client=c)
        t = await db.scalar(sa.select(CovetedTarget))
    assert out["results"][0]["status"] == "error" and "RuntimeError" in t.last_error


async def test_handlers_chain_score_into_audit(maker):
    async with maker() as db:
        task = Task(kind="score_coveted", payload={})
        db.add(task)
        await db.commit()
        out = await handlers.score_coveted(db, None, task)
        kinds = (await db.scalars(sa.select(Task.kind).where(Task.status == "queued"))).all()
        quiet = Task(kind="score_coveted", payload={"no_audit": True})
        db.add(quiet)
        await db.commit()
        await handlers.score_coveted(db, None, quiet)
        n = await db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.kind == "audit_coverage"))
    assert out["scored"] == 0 and "audit_coverage" in kinds and n == 1
    assert {"score_coveted", "audit_coverage"} <= set(handlers.HANDLERS)


async def test_target_resolves_from_a_link_on_its_careers_page(maker):
    page = httpx.Response(200, html='<a href="https://boards.greenhouse.io/acme/jobs/1">Apply</a>')
    async with maker() as db, site({"https://acme.com/careers": page}) as c:
        t = CovetedTarget(name="Acme", domain="acme.com", source="admin")
        db.add(t)
        await db.commit()
        out = await coveted.audit_coverage(db, client=c)
        await db.refresh(t)
        company = await db.get(Company, t.company_id)
    assert out["resolved"] == 1 and out["results"][0]["rung"] == "embedded"
    assert (company.ats, company.slug) == ("greenhouse", "acme")


async def test_another_broken_board_is_not_a_fix_either(maker):
    routes = {"https://acme.com/careers": redirect("https://boards.greenhouse.io/acme")}
    async with maker() as db, site(routes) as c:
        db.add(Company(name="Acme old", ats="greenhouse", slug="acme", enabled=False))
        t = CovetedTarget(name="Acme", domain="acme.com", source="admin")
        db.add(t)
        await db.commit()
        out = await coveted.audit_coverage(db, client=c)
        await db.refresh(t)
    assert out["resolved"] == 0 and t.company_id is None and "isn't working either" in t.last_error


async def test_refused_sites_are_never_fetched(maker):
    calls: list[str] = []
    async with maker() as db, site({}, calls) as c:
        t = CovetedTarget(name="Apple", domain="apple.com", source="admin")
        db.add(t)
        await db.commit()
        await coveted.audit_coverage(db, client=c)
        await db.refresh(t)
    assert calls == [] and "We don't read Apple" in t.last_error


async def test_a_guessed_domain_owned_by_another_company_is_left_alone(maker):
    async with maker() as db:
        a = Company(name="Acme", ats="greenhouse", slug="acme", enabled=False, coveted_tier=1)
        b = Company(name="Acme", ats="lever", slug="acme", enabled=False, coveted_tier=1)
        db.add_all([a, b])
        await db.commit()
        assert (await coveted._target_for(db, a)).company_id == a.id
        assert await coveted._target_for(db, b) is None
