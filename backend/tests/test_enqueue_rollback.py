"""Requests that queue a task (#48): `ledger.add` only flushes, so the task commits with the request's
other writes, and a request that fails after it keeps no task and publishes nothing."""
import pytest

from jobfinder.config import get_settings
from jobfinder.models import Company, CovetedTarget, Task, User
from jobfinder.tasks import ledger
from tests.rollback import Boom, count, raise_after
from tests.test_cover_letters_api import setup


async def _uid(client) -> int:
    return (await client.get("/api/auth/me")).json()["id"]


async def _fails_after_add(client, monkeypatch, method, path, json=None):
    """Make the request fail right after its ledger.add, and expect the 500 to surface as Boom."""
    raise_after(monkeypatch, ledger, "add")
    with pytest.raises(Boom):
        await client.request(method, path, json=json)


async def _company(maker, **kw) -> int:
    async with maker() as db:
        c = Company(name="Acme", ats="greenhouse", slug="acme", **kw)
        db.add(c)
        await db.commit()
        return c.id


# ── the task commits with the request ───────────────────────────────────
async def test_a_queued_task_commits_and_publishes_with_the_request(authed, maker, published):
    await authed.put("/api/profile/status", json={"salary_min": 90000})
    r = await authed.post("/api/matches/refresh")
    assert r.status_code == 202
    tid = r.json()["task_id"]
    assert await count(maker, Task, id=tid, kind="match_user", queue="ai", status="queued") == 1
    assert await published.settle() == [("ai", "match_user", tid)]


# ── user endpoints ──────────────────────────────────────────────────────
async def test_interview_refresh_rolls_back(authed, maker, monkeypatch, published):
    await authed.put("/api/profile/background", json={"summary": "SWE"})
    await _fails_after_add(authed, monkeypatch, "POST", "/api/profile/interview/refresh")
    assert await count(maker, Task, kind="audit_profile") == 0
    assert await published.settle() == []


async def test_upload_rolls_back_its_parse_task(authed, maker, monkeypatch, published):
    raise_after(monkeypatch, ledger, "add")
    with pytest.raises(Boom):
        await authed.post("/api/documents", files={"file": ("a.txt", b"hi", "text/plain")})
    assert await count(maker, Task, kind="parse_document") == 0
    assert await published.settle() == []


async def test_onboarding_extract_rolls_back(authed, maker, monkeypatch, published):
    monkeypatch.setattr(get_settings(), "sync_uploads", True)  # read now: no parse_document task to publish
    await authed.post("/api/documents", files={"file": ("resume.txt", b"Intern at Acme", "text/plain")})
    await _fails_after_add(authed, monkeypatch, "POST", "/api/onboarding/extract")
    assert await count(maker, Task, kind="extract_profile") == 0
    assert await published.settle() == []


async def test_onboarding_followups_roll_back(authed, maker, monkeypatch, published):
    await authed.put("/api/profile/background", json={"summary": "SWE"})
    await _fails_after_add(authed, monkeypatch, "POST", "/api/onboarding/followups")
    assert await count(maker, Task, kind="followup_questions") == 0
    assert await published.settle() == []


async def test_matches_refresh_rolls_back(authed, maker, monkeypatch, published):
    await authed.put("/api/profile/status", json={"salary_min": 90000})
    await _fails_after_add(authed, monkeypatch, "POST", "/api/matches/refresh")
    assert await count(maker, Task, kind="match_user") == 0
    assert await published.settle() == []


@pytest.mark.parametrize("path, kind", [("/cover-letter", "cover_letter"), ("/resume", "resume_tailor")])
async def test_drafting_rolls_back(authed, maker, monkeypatch, published, path, kind):
    mid, _ = await setup(authed, maker)
    await _fails_after_add(authed, monkeypatch, "POST", f"/api/matches/{mid}{path}", json={})
    assert await count(maker, Task, kind=kind) == 0
    assert await published.settle() == []


async def test_job_link_rolls_back_with_its_task(authed, maker, monkeypatch, published):
    from jobfinder.models import JobLink, RateEvent

    monkeypatch.setattr(get_settings(), "job_links_enabled", True)
    # an unknown board: the link is saved as resolving and its resolve task queued
    await _fails_after_add(authed, monkeypatch, "POST", "/api/job-links",
                           json={"url": "https://boards.greenhouse.io/newco/jobs/1"})
    assert await count(maker, JobLink) == 0
    assert await count(maker, RateEvent, kind="job_link") == 0
    assert await count(maker, Task, kind="resolve_job_link") == 0
    assert await published.settle() == []


async def test_settings_that_queue_a_rescore_roll_back(authed, maker, monkeypatch, published):
    uid = await _uid(authed)
    await authed.put("/api/profile/status", json={"salary_min": 90000})
    body = {"digest_enabled": True, "digest_hour": 11, "timezone": "UTC", "question_emails_enabled": True,
            "match_budget_enabled": True, "match_budget": get_settings().match_daily_llm_budget + 10}
    await _fails_after_add(authed, monkeypatch, "PUT", "/api/settings", json=body)
    assert await count(maker, User, id=uid, digest_hour=11) == 0
    assert await count(maker, Task, kind="match_user") == 0
    assert await published.settle() == []


# ── admin endpoints ─────────────────────────────────────────────────────
async def test_pinning_a_new_employer_rolls_back_with_its_audit(client, maker, make_account, monkeypatch, published):
    await make_account("owner@x.com", admin=True)
    await _fails_after_add(client, monkeypatch, "POST", "/api/admin/coverage/pin",
                           json={"name": "Acme", "domain": "acme.com"})
    assert await count(maker, CovetedTarget) == 0
    assert await count(maker, Task, kind="audit_coverage") == 0
    assert await published.settle() == []


async def test_unpinning_a_company_rolls_back_with_its_rescore(client, maker, make_account, monkeypatch, published):
    cid = await _company(maker, coveted_tier=1, coveted_score=50, coveted_pinned=True)
    await make_account("owner@x.com", admin=True)
    await _fails_after_add(client, monkeypatch, "POST", "/api/admin/coverage/unpin",
                           json={"company_id": cid, "exclude": True})
    assert await count(maker, Company, id=cid, coveted_pinned=True) == 1
    assert await count(maker, Task, kind="score_coveted") == 0
    assert await published.settle() == []


@pytest.mark.parametrize("path, kind", [
    ("/rescore", "score_coveted"), ("/targets/{tid}/resolve", "audit_coverage"), ("/{cid}/resolve", "audit_coverage"),
])
async def test_coverage_actions_roll_back(client, maker, make_account, monkeypatch, published, path, kind):
    cid = await _company(maker)
    async with maker() as db:
        t = CovetedTarget(name="Wanted", domain="wanted.com", source="admin", score=100)
        db.add(t)
        await db.commit()
    await make_account("owner@x.com", admin=True)
    await _fails_after_add(client, monkeypatch, "POST", "/api/admin/coverage" + path.format(tid=t.id, cid=cid))
    assert await count(maker, Task, kind=kind) == 0
    assert await published.settle() == []


async def test_ai_check_rolls_back(client, maker, make_account, monkeypatch, published):
    await make_account("owner@x.com", admin=True)
    await _fails_after_add(client, monkeypatch, "POST", "/api/admin/ai/check")
    assert await count(maker, Task, kind="ai_check") == 0
    assert await published.settle() == []
