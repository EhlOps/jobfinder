import pytest
import sqlalchemy as sa
from factories import make_match, make_question
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.models import Profile, ProfileFact, Task, User
from jobfinder.notify import email as mail
from jobfinder.notify.email import EmailError
from jobfinder.notify.tokens import MAX_AGE, make_token


@pytest.fixture
def maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def uid(client):
    return (await client.get("/api/auth/me")).json()["id"]


# ── logged-in questions ──────────────────────────────────────────────────
async def test_list_and_answer_while_logged_in(authed, maker):
    me = await uid(authed)
    async with maker() as db:
        m, job = await make_match(db, me)
        await make_question(db, me, "Have you used Kafka?", match_ids=[m.id])
        await make_question(db, me, "Done already", status="answered")
    r = (await authed.get("/api/questions")).json()["questions"]
    assert len(r) == 1 and r[0]["question"] == "Have you used Kafka?" and r[0]["jobs"][0]["title"] == job.title

    res = await authed.post("/api/questions/answers", json={"answers": [{"id": r[0]["id"], "answer": "Yes, in a class project"}]})
    assert res.json() == {"answered": 1, "skipped": 0, "remaining": 0}
    assert (await authed.get("/api/questions")).json()["questions"] == []
    facts = (await authed.get("/api/profile/facts")).json()
    assert [(f["question"], f["answer"], f["source"]) for f in facts] == [("Have you used Kafka?", "Yes, in a class project", "job_question")]
    assert (await authed.get("/api/profile")).json()["version"] == 2           # profile changed -> matches become stale


async def test_answer_validation(authed):
    assert (await authed.post("/api/questions/answers", json={"answers": [{"id": 1, "answer": "x" * 2001}]})).status_code == 422
    assert (await authed.post("/api/questions/answers", json={"answers": [{"id": i} for i in range(31)]})).status_code == 422
    assert (await authed.post("/api/questions/answers", json={"answers": []})).json()["answered"] == 0


async def test_questions_require_login(client):
    assert (await client.get("/api/questions")).status_code == 401
    assert (await client.post("/api/questions/answers", json={"answers": []})).status_code == 401


# ── via the signed link (no login) ───────────────────────────────────────
async def test_token_flow_needs_no_login_and_only_touches_that_user(authed, client, maker):
    me = await uid(authed)
    async with maker() as db:
        mine = await make_question(db, me, "Mine?")
    token = make_token("questions", me)
    await client.post("/api/auth/logout")                                       # prove it works while logged out
    r = await client.get(f"/api/q/{token}")
    assert r.status_code == 200 and [q["question"] for q in r.json()["questions"]] == ["Mine?"]
    res = await client.post(f"/api/q/{token}/answers", json={"answers": [{"id": mine.id, "answer": "Yes"}]})
    assert res.json()["answered"] == 1
    async with maker() as db:
        assert (await db.scalars(sa.select(ProfileFact.answer))).one() == "Yes"
        assert [t.kind for t in await db.scalars(sa.select(Task))] == ["match_user"]      # re-scoring queued


async def test_token_cannot_reach_another_users_questions(authed, client, maker):
    me = await uid(authed)
    async with maker() as db:
        other = User(email="victim@x.com", password_hash="x", timezone="UTC")
        db.add(other)
        await db.flush()
        theirs = await make_question(db, other.id, "Private?")
    token = make_token("questions", me)
    assert (await client.get(f"/api/q/{token}")).json()["questions"] == []
    res = await client.post(f"/api/q/{token}/answers", json={"answers": [{"id": theirs.id, "answer": "x"}]})
    assert res.json()["answered"] == 0


async def test_bad_tokens(client, monkeypatch):
    for bad in ("garbage", make_token("unsubscribe", 1), make_token("questions", 1)[:-2] + "zz"):
        assert (await client.get(f"/api/q/{bad}")).status_code == 400
        assert (await client.post(f"/api/q/{bad}/answers", json={"answers": []})).status_code == 400
    t = make_token("questions", 1)
    monkeypatch.setitem(MAX_AGE, "questions", -1)
    r = await client.get(f"/api/q/{t}")
    assert r.status_code == 410 and "expired" in r.json()["detail"]


# ── unsubscribe ──────────────────────────────────────────────────────────
async def test_get_unsubscribe_only_asks_and_changes_nothing(authed, client):
    token = make_token("unsubscribe", await uid(authed))
    r = await client.get(f"/api/unsubscribe/{token}")  # what an email scanner or link previewer does
    assert r.status_code == 200 and "<form" in r.text and f'action="/api/unsubscribe/{token}"' in r.text
    assert "unsubscribed" not in r.text.lower().replace("unsubscribe</button>", "")
    s = (await client.get("/api/settings")).json()
    assert s["digest_enabled"] is True and s["question_emails_enabled"] is True


async def test_post_unsubscribe_disables_both_emails(authed, client):
    token = make_token("unsubscribe", await uid(authed))
    # one-click (RFC 8058) sends this exact body; the confirmation form sends an empty one
    r = await client.post(f"/api/unsubscribe/{token}", content="List-Unsubscribe=One-Click",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and "unsubscribed" in r.text.lower() and r.headers["content-type"].startswith("text/html")
    s = (await client.get("/api/settings")).json()
    assert s["digest_enabled"] is False and s["question_emails_enabled"] is False


async def test_unsubscribe_rejects_other_tokens(client):
    for method in ("get", "post"):
        assert (await getattr(client, method)("/api/unsubscribe/garbage")).status_code == 400
        assert (await getattr(client, method)(f"/api/unsubscribe/{make_token('questions', 1)}")).status_code == 400


# ── settings ─────────────────────────────────────────────────────────────
async def test_settings_read_update_and_validation(authed):
    s = (await authed.get("/api/settings")).json()
    assert s == {"digest_enabled": True, "digest_hour": 8, "timezone": "UTC", "question_emails_enabled": True,
                 "match_budget_enabled": True, "match_budget": 25}
    new = {"digest_enabled": False, "digest_hour": 17, "timezone": "America/New_York", "question_emails_enabled": True,
           "match_budget_enabled": False, "match_budget": 40}
    assert (await authed.put("/api/settings", json=new)).json() == new
    assert (await authed.get("/api/settings")).json() == new
    for bad in ({**new, "digest_hour": 24}, {**new, "digest_hour": -1}, {**new, "timezone": "Mars/Base"}, {**new, "timezone": ""},
                {**new, "match_budget": 0}, {**new, "match_budget": 1001}):
        assert (await authed.put("/api/settings", json=bad)).status_code == 422
    assert (await authed.get("/api/settings")).json() == new


async def test_raising_the_match_budget_queues_a_run(authed, maker):
    me = await uid(authed)
    async with maker() as db:
        profile = await db.get(Profile, me)
        if profile is None:
            db.add(Profile(user_id=me, status={"target_roles": ["x"]}, background={}, version=1))
        else:
            profile.status = {"target_roles": ["x"]}
        await db.commit()
    base = (await authed.get("/api/settings")).json()

    async def queued():
        async with maker() as db:
            return await db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.kind == "match_user"))

    await authed.put("/api/settings", json={**base, "match_budget": 10})  # lowering: nothing to start
    assert await queued() == 0
    await authed.put("/api/settings", json={**base, "match_budget": 50})
    assert await queued() == 1
    await authed.put("/api/settings", json={**base, "match_budget": 50, "match_budget_enabled": False})  # off: more room
    assert await queued() == 1  # deduped with the run already waiting
    s = (await authed.get("/api/settings")).json()
    assert s["match_budget_enabled"] is False and s["match_budget"] == 50


async def test_settings_require_login(client):
    assert (await client.get("/api/settings")).status_code == 401
    assert (await client.post("/api/settings/digest/preview")).status_code == 401


async def test_preview_email(authed, maker, monkeypatch):
    sent = []

    async def fake(to, subject, text, html=None, headers=None):
        sent.append((to, subject))

    monkeypatch.setattr(mail, "send_email", fake)
    r = await authed.post("/api/settings/digest/preview")
    assert r.status_code == 409 and sent == []                                  # nothing to say yet

    me = await uid(authed)
    async with maker() as db:
        await make_match(db, me, score=72)
        await make_question(db, me)
    r = await authed.post("/api/settings/digest/preview")
    assert r.status_code == 200 and r.json()["matches"] == 1 and r.json()["questions"] == 1
    assert sent[0][1].startswith("[Preview]") and sent[0][0].endswith("@example.com")

    async def boom(*a, **k):
        raise EmailError("smtp down")

    monkeypatch.setattr(mail, "send_email", boom)
    r = await authed.post("/api/settings/digest/preview")
    assert r.status_code == 502 and "smtp down" in r.json()["detail"]
    async with maker() as db:                                                   # a preview never marks anything as sent
        u = await db.get(User, me)
        assert u.last_digest_at is None


# ── profile facts: delete ────────────────────────────────────────────────
async def test_delete_fact_bumps_version_and_checks_owner(authed, client, make_account):
    f = (await authed.post("/api/profile/facts", json={"question": "q", "answer": "a"})).json()
    v = (await authed.get("/api/profile")).json()["version"]
    assert (await authed.delete(f"/api/profile/facts/{f['id']}")).status_code == 204
    assert (await authed.get("/api/profile")).json()["version"] == v + 1 and (await authed.get("/api/profile/facts")).json() == []
    assert (await authed.delete(f"/api/profile/facts/{f['id']}")).status_code == 404
    g = (await authed.post("/api/profile/facts", json={"question": "q2", "answer": "a2"})).json()
    await client.post("/api/auth/logout")
    await make_account("o@x.com", "password-1234")
    assert (await client.delete(f"/api/profile/facts/{g['id']}")).status_code == 404
