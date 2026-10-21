"""One transaction per request and per task (#45): a write in the matches, cover letter and resume area
that fails part-way leaves nothing behind, and a matching run keeps each job it scored.

The other endpoints here only read, or only queue a task (their rollback tests are in
test_enqueue_rollback), or only write to the file store (resumes); their status tests live in
test_matches_api, test_cover_letters_api and test_resumes_api.
"""
import pytest

from jobfinder.ai.claude_code import AIError
from jobfinder.core.config import get_settings
from jobfinder.models import CoverLetter, JobMatch, Profile, ProfileFact, Task
from jobfinder.repositories import letters as letters_repo
from jobfinder.scheduling import queue
from jobfinder.services import profile as profile_service
from jobfinder.worker import run_task
from tests.fakes import FakeLetterAI, FakeScoringAI
from tests.rollback import Boom, count, raise_after
from tests.test_cover_letters_api import LETTER, draft, setup
from tests.test_matching_service import make_job
from tests.test_matching_service import make_user as make_matching_user


async def _version(maker, uid):
    async with maker() as db:
        return (await db.get(Profile, uid)).version


async def test_answers_roll_back_with_the_version_bump(authed, maker, monkeypatch):
    mid, _ = await setup(authed, maker)
    uid = (await authed.get("/api/auth/me")).json()["id"]
    before = await _version(maker, uid)
    raise_after(monkeypatch, profile_service, "bump_version")
    with pytest.raises(Boom):
        await authed.post(f"/api/matches/{mid}/answers", json={"answers": [{"question": "Go?", "answer": "Yes"}]})
    assert await count(maker, ProfileFact, user_id=uid, source="job_question") == 0
    assert await _version(maker, uid) == before
    assert await count(maker, Task, kind="match_user") == 0


async def test_answers_save_facts_bump_the_version_and_queue_a_rescore(authed, maker):
    mid, job_id = await setup(authed, maker)
    uid = (await authed.get("/api/auth/me")).json()["id"]
    before = await _version(maker, uid)
    r = await authed.post(f"/api/matches/{mid}/answers", json={"answers": [{"question": "Go?", "answer": "Yes"}]})
    assert r.status_code == 202 and r.json() == {"saved": 1}
    assert await count(maker, ProfileFact, user_id=uid, source="job_question", job_id=job_id) == 1
    assert await _version(maker, uid) == before + 1
    assert await count(maker, Task, kind="match_user", status="queued") == 1


async def test_status_change_rolls_back(authed, maker, monkeypatch):
    mid, _ = await setup(authed, maker)
    raise_after(monkeypatch, letters_repo, "job_ids_with_letters")  # runs after the status is flushed
    with pytest.raises(Boom):
        await authed.patch(f"/api/matches/{mid}", json={"status": "saved"})
    assert await count(maker, JobMatch, id=mid, status="new") == 1


async def test_letter_edit_rolls_back(authed, maker, monkeypatch):
    mid, job_id = await setup(authed, maker)
    await draft(authed, maker, mid)
    raise_after(monkeypatch, letters_repo, "save")
    with pytest.raises(Boom):
        await authed.put(f"/api/matches/{mid}/cover-letter", json={"content": "My own words."})
    assert await count(maker, CoverLetter, job_id=job_id, content=LETTER, edited=False) == 1


async def test_letter_delete_rolls_back(authed, maker, monkeypatch):
    mid, job_id = await setup(authed, maker)
    await draft(authed, maker, mid)
    raise_after(monkeypatch, letters_repo, "delete")
    with pytest.raises(Boom):
        await authed.delete(f"/api/matches/{mid}/cover-letter")
    assert await count(maker, CoverLetter, job_id=job_id) == 1


async def test_a_failed_letter_task_keeps_no_draft(authed, maker, monkeypatch):
    mid, job_id = await setup(authed, maker)
    r = await authed.post(f"/api/matches/{mid}/cover-letter", json={})
    raise_after(monkeypatch, letters_repo, "upsert_draft")
    await run_task(r.json()["task_id"], FakeLetterAI(LETTER), maker)
    assert await count(maker, CoverLetter, job_id=job_id) == 0
    assert await count(maker, Task, id=r.json()["task_id"], status="failed") == 1


@pytest.mark.parametrize("path", ["/cover-letter", "/resume"])
async def test_missing_background_is_one_error(authed, maker, path):
    mid, _ = await setup(authed, maker, background=False)
    r = await authed.post(f"/api/matches/{mid}{path}", json={})
    assert r.status_code == 422
    assert r.json() == {"detail": "Finish your profile first (onboarding)", "code": "profile_incomplete"}


async def test_matching_task_keeps_each_scored_job_when_a_rate_limit_stops_it(maker, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "match_daily_llm_budget", 25)
    monkeypatch.setattr(s, "ai_max_concurrency", 2)
    async with maker() as db:
        u = await make_matching_user(db, email="rl@x.com")
        for t in ("Backend Engineer A", "Backend Engineer B", "Backend Engineer C", "Backend Engineer D"):
            await make_job(db, t)  # equal scores: the newest (D, C) form the first batch
        task = await queue.enqueue(db, "match_user", user_id=u.id)
    limited = AIError("rate_limited", "usage limit")
    ai = FakeScoringAI(errors={"Backend Engineer A": limited, "Backend Engineer B": limited})
    await run_task(task.id, ai, maker)
    assert await count(maker, JobMatch, user_id=u.id) == 2  # committed by the handler's checkpoint
    async with maker() as db:
        assert (await db.get(Profile, u.id)).match_summary["scored"] == 2
        assert (await db.get(Task, task.id)).status == "queued"  # retried later
