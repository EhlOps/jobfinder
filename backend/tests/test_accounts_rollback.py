"""Accounts, settings and tasks endpoints commit once per request: a failure after the first write
keeps nothing (#44). Endpoints with no request-session writes get a status test instead."""
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from jobfinder.config import get_settings
from jobfinder.models import Profile, RateEvent, Session, Task, User
from jobfinder.repositories import users
from jobfinder.services import accounts, ratelimit
from jobfinder.services.tokens import make_password_token
from tests.rollback import Boom, count, raise_after


async def _user(maker, email="sam@example.com") -> User:
    async with maker() as db:
        return (await db.scalars(sa.select(User).where(User.email == email))).one()


async def test_login_keeps_no_session_when_it_fails_after_creating_one(client, make_account, maker, monkeypatch):
    await make_account(login=False)
    raise_after(monkeypatch, accounts, "create_session")
    with pytest.raises(Boom):
        await client.post("/api/auth/login", json={"email": "sam@example.com", "password": "correct-horse-battery"})
    assert await count(maker, Session) == 0


async def test_link_request_keeps_no_rate_event_when_it_fails(client, make_account, maker, monkeypatch, sent_mail):
    await make_account(login=False)
    raise_after(monkeypatch, ratelimit, "hit")
    with pytest.raises(Boom):
        await client.post("/api/auth/password-reset/request", json={"email": "sam@example.com"})
    assert await count(maker, RateEvent) == 0
    assert sent_mail == []


async def test_confirm_keeps_no_password_or_profile_when_it_fails(client, make_account, maker, monkeypatch):
    await make_account("new@x.com", pending=True, login=False)
    uid = (await _user(maker, "new@x.com")).id
    raise_after(monkeypatch, users, "delete_sessions_of")
    with pytest.raises(Boom):
        await client.post(
            "/api/auth/password-reset/confirm",
            json={"token": make_password_token(uid, None), "password": "a-brand-new-password"},
        )
    u = await _user(maker, "new@x.com")
    assert u.password_hash is None and u.activated_at is None
    assert await count(maker, Profile, user_id=uid) == 0
    assert await count(maker, Session, user_id=uid) == 0


async def test_logout_keeps_the_session_when_it_fails(client, make_account, maker, monkeypatch):
    await make_account()
    raise_after(monkeypatch, users, "delete_session")
    with pytest.raises(Boom):
        await client.post("/api/auth/logout")
    assert await count(maker, Session) == 1


async def test_settings_write_keeps_nothing_when_it_fails(authed, maker, monkeypatch):
    monkeypatch.setattr(get_settings(), "match_daily_llm_budget", 25)  # so 999 is a bigger budget
    raise_after(monkeypatch, users, "profile_of")  # read only when the budget grows, after the user's fields change
    body = {"digest_enabled": False, "digest_hour": 21, "timezone": "Europe/Berlin",
            "question_emails_enabled": False, "match_budget_enabled": True, "match_budget": 999}
    with pytest.raises(Boom):
        await authed.put("/api/settings", json=body)
    u = await _user(maker)
    assert (u.digest_enabled, u.digest_hour, u.timezone, u.match_budget) == (True, 8, "UTC", None)


async def test_settings_write_is_committed_by_the_request(authed, maker):
    body = {"digest_enabled": False, "digest_hour": 21, "timezone": "Europe/Berlin", "question_emails_enabled": False}
    assert (await authed.put("/api/settings", json=body)).status_code == 200
    u = await _user(maker)
    assert (u.digest_enabled, u.digest_hour, u.timezone) == (False, 21, "Europe/Berlin")


async def test_me_records_last_seen_through_the_request_commit(authed, maker):
    async with maker() as db:
        await db.execute(sa.update(User).values(last_seen_at=None))
        await db.commit()
    assert (await authed.get("/api/auth/me")).status_code == 200
    seen = (await _user(maker)).last_seen_at
    assert seen is not None and datetime.now(UTC) - seen < timedelta(minutes=1)


async def test_me_and_admin_only_answer_401_and_403(client, make_account):
    r = await client.get("/api/auth/me")
    assert (r.status_code, r.json()["detail"]) == (401, "Not authenticated")
    await make_account()
    r = await client.get("/api/admin/coverage")
    assert (r.status_code, r.json()["detail"]) == (403, "Admin only")


async def test_tasks_are_readable_only_by_their_owner(authed, make_account, maker):
    other = await make_account("other@x.com", login=False)
    async with maker() as db:
        mine = Task(kind="x", user_id=(await _user(maker)).id)
        theirs = Task(kind="x", user_id=(await _user(maker, other["email"])).id)
        db.add_all([mine, theirs])
        await db.commit()
    assert (await authed.get(f"/api/tasks/{mine.id}")).status_code == 200
    r = await authed.get(f"/api/tasks/{theirs.id}")
    assert (r.status_code, r.json()["detail"]) == (404, "Not found")
    assert (await authed.get("/api/tasks/999999")).status_code == 404
