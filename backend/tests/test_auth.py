from jobfinder.auth import security


async def test_login_me_logout(client, creds, make_account):
    await make_account(creds["email"], creds["password"], login=False)
    r = await client.post("/api/auth/login", json={"email": "Sam@Example.com", "password": creds["password"]})
    assert r.status_code == 200 and r.json()["email"] == "sam@example.com"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert (await client.get("/api/auth/me")).json()["email"] == "sam@example.com"

    assert (await client.post("/api/auth/logout")).status_code == 204
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_signup_is_gone(client):
    r = await client.post("/api/auth/signup", json={"email": "a@b.com", "password": "password-1234"})
    assert r.status_code in (404, 405)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_failed_logins_are_indistinguishable(client, make_account, monkeypatch):
    await make_account("known@x.com", "right-password-1", login=False)
    await make_account("pending@x.com", pending=True)
    calls = []
    real = security.verify_password
    monkeypatch.setattr(security, "verify_password", lambda h, p: calls.append(h) or real(h, p))
    out = []
    for email, pw in (("known@x.com", "wrong-password-1"), ("nobody@x.com", "whatever-pass-1"), ("pending@x.com", "whatever-pass-1")):
        r = await client.post("/api/auth/login", json={"email": email, "password": pw})
        out.append((r.status_code, r.json()))
    assert out[0] == out[1] == out[2] == (401, {"detail": "Invalid email or password"})
    assert len(calls) == 3  # one argon2 verification each, even with no password hash to check


async def test_requires_auth(client):
    for path in ("/api/profile", "/api/documents", "/api/links", "/api/profile/facts"):
        assert (await client.get(path)).status_code == 401


async def test_login_is_rate_limited_per_email(client, make_account):
    await make_account("known@x.com", "right-password-1", login=False)
    for _ in range(10):
        assert (await client.post("/api/auth/login", json={"email": "known@x.com", "password": "bad-password-1"})).status_code == 401
    r = await client.post("/api/auth/login", json={"email": "known@x.com", "password": "right-password-1"})
    assert r.status_code == 429


async def test_login_is_rate_limited_per_ip(client, monkeypatch):
    from jobfinder.config import get_settings

    monkeypatch.setattr(get_settings(), "login_failures_per_ip", 3)
    for i in range(3):
        assert (await client.post("/api/auth/login", json={"email": f"u{i}@x.com", "password": "bad-password-1"})).status_code == 401
    assert (await client.post("/api/auth/login", json={"email": "u9@x.com", "password": "bad-password-1"})).status_code == 429


async def test_only_a_hash_of_the_session_token_is_stored(client, make_account, engine):
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from jobfinder.models import Session

    await make_account("hash@x.com")
    raw = client.cookies.get("session")
    async with async_sessionmaker(engine)() as db:
        stored = (await db.scalars(sa.select(Session.token))).all()
    assert stored == [security.hash_session_token(raw)] and raw not in stored and len(stored[0]) == 64
    assert (await client.get("/api/auth/me")).status_code == 200
    client.cookies.set("session", stored[0])  # presenting the stored hash as a token must not work
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_logout_removes_the_session_row_and_expired_ones_are_pruned(client, make_account, engine):
    from datetime import UTC, datetime, timedelta

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from jobfinder.models import Session, User

    await make_account("out@x.com")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        uid = (await db.scalars(sa.select(User.id))).one()
        db.add(Session(token="old" * 20, user_id=uid, expires_at=datetime.now(UTC) - timedelta(days=1)))
        await db.commit()
        await security.prune_sessions(db)
        assert (await db.scalar(sa.select(sa.func.count()).select_from(Session))) == 1  # only the live one is left
    await client.post("/api/auth/logout")
    async with maker() as db:
        assert (await db.scalar(sa.select(sa.func.count()).select_from(Session))) == 0
