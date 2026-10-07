import re
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.auth.security import hash_session_token
from jobfinder.config import Settings, get_settings
from jobfinder.models import Profile, Session, User
from jobfinder.notify import tokens
from jobfinder.notify.tokens import make_password_token, make_token

REQ = "/api/auth/password-reset/request"
CONF = "/api/auth/password-reset/confirm"
NEW_PW = "a-brand-new-password"


def token_in(sent) -> str:
    return re.search(r"#token=(\S+)", sent[-1][2]).group(1)


async def _user(engine, email):
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        return (await db.scalars(sa.select(User).where(User.email == email))).one()


async def test_request_is_identical_for_known_and_unknown_and_only_known_gets_mail(client, make_account, sent_mail):
    await make_account("known@x.com", pending=True)
    a = await client.post(REQ, json={"email": "known@x.com"})
    b = await client.post(REQ, json={"email": "nobody@x.com"})
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert [m[0] for m in sent_mail] == ["known@x.com"]
    assert sent_mail[0][1] == "Set your JobFinder password"


async def test_active_account_gets_reset_subject(client, make_account, sent_mail):
    await make_account("act@x.com", login=False)
    await client.post(REQ, json={"email": "ACT@x.com"})
    assert sent_mail[0][1] == "Reset your JobFinder password"


async def test_confirm_activates_pending_row_and_signs_in(client, make_account, sent_mail, engine):
    await make_account("new@x.com", pending=True)
    await client.post(REQ, json={"email": "new@x.com"})
    r = await client.post(CONF, json={"token": token_in(sent_mail), "password": NEW_PW, "timezone": "America/New_York"})
    assert r.status_code == 200 and r.json()["timezone"] == "America/New_York"
    assert (await client.get("/api/auth/me")).json()["email"] == "new@x.com"
    u = await _user(engine, "new@x.com")
    assert (u.digest_hour, u.digest_enabled, u.question_emails_enabled, u.is_admin) == (8, True, True, False)
    assert u.activated_at and u.password_hash
    async with async_sessionmaker(engine)() as db:
        p = await db.get(Profile, u.id)
        assert p.status == {} and p.background == {} and p.version == 1
    assert (await client.post("/api/auth/logout")).status_code == 204
    assert (await client.post("/api/auth/login", json={"email": "new@x.com", "password": NEW_PW})).status_code == 200


async def test_bad_timezone_falls_back_to_utc_and_admin_from_env(client, make_account, sent_mail):
    get_settings().admin_emails = "boss@x.com"
    await make_account("boss@x.com", pending=True)
    await client.post(REQ, json={"email": "boss@x.com"})
    r = await client.post(CONF, json={"token": token_in(sent_mail), "password": NEW_PW, "timezone": "Mars/Base"})
    assert r.json()["timezone"] == "UTC" and r.json()["is_admin"] is True


async def test_link_is_single_use(client, make_account, sent_mail):
    await make_account("one@x.com", pending=True)
    await client.post(REQ, json={"email": "one@x.com"})
    tok = token_in(sent_mail)
    assert (await client.post(CONF, json={"token": tok, "password": NEW_PW})).status_code == 200
    again = await client.post(CONF, json={"token": tok, "password": "another-new-password"})
    assert again.status_code == 400


async def test_confirm_ends_other_sessions(client, make_account, sent_mail, engine):
    await make_account("sess@x.com")
    await client.post("/api/auth/logout")
    await client.post("/api/auth/login", json={"email": "sess@x.com", "password": "correct-horse-battery"})
    await client.post(REQ, json={"email": "sess@x.com"})
    u = await _user(engine, "sess@x.com")
    async with async_sessionmaker(engine)() as db:
        before = (await db.scalars(sa.select(Session.token).where(Session.user_id == u.id))).all()
    assert before
    await client.post(CONF, json={"token": token_in(sent_mail), "password": NEW_PW})
    async with async_sessionmaker(engine)() as db:
        after = (await db.scalars(sa.select(Session.token).where(Session.user_id == u.id))).all()
    assert len(after) == 1 and after[0] not in before


@pytest.mark.parametrize("bad", ["garbage", "", "x" * 50])
async def test_bad_tokens_get_one_generic_error(client, bad):
    r = await client.post(CONF, json={"token": bad or "t", "password": NEW_PW})
    assert r.status_code == 400 and "invalid or has expired" in r.json()["detail"]


async def test_wrong_purpose_token_is_rejected(client, make_account, engine):
    await make_account("wp@x.com", pending=True)
    u = await _user(engine, "wp@x.com")
    r = await client.post(CONF, json={"token": make_token("questions", u.id), "password": NEW_PW})
    assert r.status_code == 400


async def test_expired_link_is_rejected(client, make_account, engine, monkeypatch):
    await make_account("exp@x.com", pending=True)
    u = await _user(engine, "exp@x.com")
    tok = make_password_token(u.id, None)
    monkeypatch.setattr(get_settings(), "password_link_ttl_minutes", -1)
    assert (await client.post(CONF, json={"token": tok, "password": NEW_PW})).status_code == 400


async def test_password_change_invalidates_outstanding_links(client, make_account, engine):
    await make_account("chg@x.com", login=False)
    u = await _user(engine, "chg@x.com")
    old = make_password_token(u.id, u.password_hash)
    assert (await client.post(CONF, json={"token": old, "password": NEW_PW})).status_code == 200
    assert (await client.post(CONF, json={"token": old, "password": "yet-another-password"})).status_code == 400


async def test_password_rules(client, make_account, engine):
    await make_account("rules@x.com", pending=True)
    u = await _user(engine, "rules@x.com")
    tok = make_password_token(u.id, None)
    assert (await client.post(CONF, json={"token": tok, "password": "short"})).status_code == 422
    assert (await client.post(CONF, json={"token": tok, "password": "x" * 257})).status_code == 422
    r = await client.post(CONF, json={"token": tok, "password": "RULES@x.com"})
    assert r.status_code == 422


async def test_link_requests_are_rate_limited(client, make_account, sent_mail, monkeypatch):
    monkeypatch.setattr(get_settings(), "link_cooldown_seconds", 0)
    await make_account("rl@x.com", pending=True)
    for _ in range(5):
        assert (await client.post(REQ, json={"email": "rl@x.com"})).status_code == 202
    assert len(sent_mail) == 3  # 3 per hour per email; the rest answer the same but send nothing


async def test_link_cooldown_between_requests(client, make_account, sent_mail):
    await make_account("cd@x.com", pending=True)
    await client.post(REQ, json={"email": "cd@x.com"})
    await client.post(REQ, json={"email": "cd@x.com"})
    assert len(sent_mail) == 1


async def test_link_requests_are_rate_limited_per_ip(client, make_account, sent_mail, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "link_cooldown_seconds", 0)
    monkeypatch.setattr(s, "link_requests_per_ip_hour", 2)
    for i in range(4):
        await make_account(f"ip{i}@x.com", pending=True)
        await client.post(REQ, json={"email": f"ip{i}@x.com"})
    assert len(sent_mail) == 2


async def test_email_failure_does_not_change_the_response(client, make_account, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr("jobfinder.notify.email.send_email", boom)
    await make_account("down@x.com", pending=True)
    assert (await client.post(REQ, json={"email": "down@x.com"})).status_code == 202


async def test_pending_user_cannot_hold_a_session(client, make_account, engine):
    await make_account("pend@x.com", pending=True)
    u = await _user(engine, "pend@x.com")
    async with async_sessionmaker(engine)() as db:
        db.add(Session(token=hash_session_token("t" * 43), user_id=u.id, expires_at=datetime(2999, 1, 1, tzinfo=UTC)))
        await db.commit()
    client.cookies.set("session", "t" * 43)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_pending_users_get_no_daily_email_or_matching(client, make_account, engine):
    from jobfinder.matching.service import enqueue_for_active_users
    from jobfinder.notify import daily
    from tests.factories import make_user

    await make_account("pend@x.com", pending=True)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        await make_user(db, "live@x.com")
        due = await daily.due_users(db, datetime(2026, 1, 1, 8, 5, tzinfo=UTC))
        assert [u.email for u in due] == ["live@x.com"]
        await enqueue_for_active_users(db)
        pend = (await db.scalars(sa.select(User).where(User.email == "pend@x.com"))).one()
        assert daily.is_due(pend, datetime(2026, 1, 1, 8, 5, tzinfo=UTC)) is False


async def test_preview_email_is_rate_limited(authed, monkeypatch):
    monkeypatch.setattr(get_settings(), "preview_emails_per_hour", 2)
    codes = [(await authed.post("/api/settings/digest/preview")).status_code for _ in range(3)]
    assert codes[:2] != [429, 429] and codes[2] == 429


def test_secret_key_guard(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    for bad in ("", "dev-insecure-change-me", "short"):
        with pytest.raises(ValueError):
            Settings(secret_key=bad, _env_file=None)
    assert Settings(secret_key="k" * 40, _env_file=None).secret_key == "k" * 40


def test_fingerprint_empty_for_pending():
    assert tokens.password_fingerprint(None) == ""
    assert tokens.password_fingerprint("abc") != tokens.password_fingerprint("abd")


async def test_cli_add_list_and_reset_link(engine, monkeypatch, capsys):
    import argparse

    from jobfinder import cli
    from jobfinder import db as dbmod

    monkeypatch.setattr(dbmod, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    ns = argparse.Namespace(email="  New@X.com ")
    assert await cli.add_user(ns) == 0
    assert await cli.add_user(ns) == 1  # already exists
    assert await cli.list_users() == 0
    out = capsys.readouterr().out
    assert "new@x.com" in out and "pending" in out
    assert await cli.reset_link_cmd(ns) == 0
    assert "/reset-password#token=" in capsys.readouterr().out
    assert await cli.reset_link_cmd(argparse.Namespace(email="no@x.com")) == 1
