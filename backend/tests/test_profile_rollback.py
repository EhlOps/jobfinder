"""One transaction per request in the profile area (#44): a request that fails after its first write keeps
nothing, and the profile-area tasks commit at their boundary (the task, or each user of the daily tick)."""
import pytest
import sqlalchemy as sa
from factories import make_match, make_question, make_user, unknown

from jobfinder.models import ClarifyingQuestion, Profile, ProfileFact, Task, User
from jobfinder.notify import daily
from jobfinder.notify import email as mail
from jobfinder.notify.tokens import make_token
from jobfinder.repositories import profiles as profiles_repo
from jobfinder.repositories import questions as questions_repo
from jobfinder.services import profile as profile_service
from jobfinder.services import questions as questions_service
from tests.fakes import FakeQuestionsAI, run_task
from tests.rollback import Boom, count, raise_after


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    async def fake_send(to, subject, text, html=None, headers=None):
        sent.append(to)

    monkeypatch.setattr(mail, "send_email", fake_send)
    return sent


async def _uid(client) -> int:
    return (await client.get("/api/auth/me")).json()["id"]


async def _profile(maker, uid) -> Profile:
    async with maker() as db:
        return await db.get(Profile, uid)


# ── profile ─────────────────────────────────────────────────────────────
async def test_reading_a_new_profile_keeps_no_half_made_row(authed, maker, monkeypatch):
    uid = await _uid(authed)
    async with maker() as db:  # as for an account made before profiles were created with it
        await db.execute(sa.delete(Profile).where(Profile.user_id == uid))
        await db.commit()
    raise_after(monkeypatch, profiles_repo, "create")
    with pytest.raises(Boom):
        await authed.get("/api/profile")
    assert await count(maker, Profile, user_id=uid) == 0


@pytest.mark.parametrize("path, body", [
    ("/api/profile/status", {"salary_min": 90000}),
    ("/api/profile/background", {"skills": ["python"]}),
])
async def test_profile_edits_roll_back(authed, maker, monkeypatch, path, body):
    uid = await _uid(authed)
    await authed.get("/api/profile")
    raise_after(monkeypatch, profile_service, "bump_version")
    with pytest.raises(Boom):
        await authed.put(path, json=body)
    p = await _profile(maker, uid)
    assert (p.version, p.status, p.background) == (1, {}, {})


async def test_adding_a_fact_rolls_back(authed, maker, monkeypatch):
    uid = await _uid(authed)
    raise_after(monkeypatch, profile_service, "bump_version")
    with pytest.raises(Boom):
        await authed.post("/api/profile/facts", json={"question": "Used k8s?", "answer": "Yes"})
    assert await count(maker, ProfileFact, user_id=uid) == 0
    assert (await _profile(maker, uid)).version == 1


async def test_deleting_a_fact_rolls_back(authed, maker, monkeypatch):
    uid = await _uid(authed)
    fact_id = (await authed.post("/api/profile/facts", json={"question": "Used k8s?", "answer": "Yes"})).json()["id"]
    raise_after(monkeypatch, profile_service, "bump_version")
    with pytest.raises(Boom):
        await authed.delete(f"/api/profile/facts/{fact_id}")
    assert await count(maker, ProfileFact, id=fact_id) == 1
    assert (await _profile(maker, uid)).version == 2


async def test_deleting_someone_elses_fact_is_404(authed):
    r = await authed.delete("/api/profile/facts/999999")
    assert r.status_code == 404 and r.json()["detail"] == "Not found"


# ── interview ───────────────────────────────────────────────────────────
async def test_interview_answers_roll_back(authed, maker, monkeypatch):
    uid = await _uid(authed)
    await authed.put("/api/profile/background", json={"summary": "SWE"})
    async with maker() as db:
        p = await db.get(Profile, uid)
        p.audit = {"questions": [{"question": "What did you own?"}], "skipped": []}
        await db.commit()
    raise_after(monkeypatch, profile_service, "bump_version")
    with pytest.raises(Boom):
        await authed.post("/api/profile/interview/answers", json={"answers": [{"question": "What did you own?", "answer": "Billing"}]})
    assert await count(maker, ProfileFact, user_id=uid) == 0
    p = await _profile(maker, uid)
    assert p.version == 2 and p.audit["questions"] == [{"question": "What did you own?"}]
    assert await count(maker, Task, kind="audit_profile") == 0


async def test_interview_refresh_needs_a_profile(authed):
    r = await authed.post("/api/profile/interview/refresh")
    assert r.status_code == 422 and r.json()["detail"] == "Set up your profile first"


# ── questions ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("by_token", [False, True])
async def test_answering_questions_rolls_back(authed, maker, monkeypatch, by_token):
    uid = await _uid(authed)
    async with maker() as db:
        q = await make_question(db, uid)
    raise_after(monkeypatch, profile_service, "bump_version")
    await authed.get("/api/profile")  # a profile to bump
    url = f"/api/q/{make_token('questions', uid)}/answers" if by_token else "/api/questions/answers"
    with pytest.raises(Boom):
        await authed.post(url, json={"answers": [{"id": q.id, "answer": "Yes"}]})
    assert await count(maker, ProfileFact, user_id=uid) == 0
    assert await count(maker, ClarifyingQuestion, id=q.id, status="pending") == 1
    assert await count(maker, Task, kind="match_user") == 0


async def test_unsubscribe_rolls_back(authed, client, maker, monkeypatch):
    uid = await _uid(authed)
    raise_after(monkeypatch, questions_service, "unsubscribe")
    with pytest.raises(Boom):
        await client.post(f"/api/unsubscribe/{make_token('unsubscribe', uid)}")
    assert await count(maker, User, id=uid, digest_enabled=True, question_emails_enabled=True) == 1


# ── tasks ───────────────────────────────────────────────────────────────
async def _run(maker, kind, user_id=None, ai=None) -> Task:
    async with maker() as db:
        t = Task(kind=kind, payload={}, user_id=user_id, status="queued")
        db.add(t)
        await db.commit()
        tid = t.id
    await run_task(tid, ai or FakeQuestionsAI(), maker)
    async with maker() as db:
        return await db.get(Task, tid)


async def test_collect_questions_task_commits_at_its_boundary(maker):
    async with maker() as db:
        u = await make_user(db)
        await make_match(db, u.id, confidence=0.4, unknowns=[unknown("Kafka?")])
    task = await _run(maker, "collect_questions", u.id)
    assert task.status == "done" and task.result["new_questions"] == 1
    assert await count(maker, ClarifyingQuestion, user_id=u.id) == 1


async def test_collect_questions_task_failure_keeps_nothing(maker, monkeypatch):
    async with maker() as db:
        u = await make_user(db)
        m, _ = await make_match(db, u.id, confidence=0.4, unknowns=[unknown("Kafka?")])
    raise_after(monkeypatch, questions_repo, "mark_reviewed")
    task = await _run(maker, "collect_questions", u.id)
    assert task.status != "done"
    assert await count(maker, ClarifyingQuestion, user_id=u.id) == 0
    async with maker() as db:
        assert (await db.scalar(sa.text("select questions_collected from job_matches where id = :i"), {"i": m.id})) is False


async def test_daily_tick_isolates_a_crashing_user(maker, monkeypatch, outbox):
    from datetime import UTC, datetime

    hour = datetime.now(UTC).hour
    async with maker() as db:
        bad = await make_user(db, "a-bad@x.com", hour=hour)  # first, so its rollback expires the user loaded next
        ok = await make_user(db, "b-ok@x.com", hour=hour)
        for u in (ok, bad):
            await make_question(db, u.id, f"Kafka for {u.id}?")  # collected ahead of the hour (#50)
    real = daily.send_daily

    async def send(db, user, **kw):
        if user.id == bad.id:
            raise RuntimeError("template blew up")
        return await real(db, user, **kw)

    monkeypatch.setattr(daily, "send_daily", send)
    task = await _run(maker, "daily_emails")
    assert task.result["sent"] == 1 and set(task.result["errors"]) == {str(bad.id)}
    assert await count(maker, ClarifyingQuestion, user_id=ok.id, status="emailed") == 1
    # The bad user's crash rolled back only their claim: their questions wait for tomorrow.
    assert await count(maker, ClarifyingQuestion, user_id=bad.id, status="pending") == 1
