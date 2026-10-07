import os

from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.ai import credentials
from jobfinder.ai.claude_code import AIError
from jobfinder.config import get_settings
from jobfinder.models import AICall
from jobfinder.worker import run_task

TOKEN = "sk-ant-oat01-" + "z" * 40


class FakeAI:
    def __init__(self, error: AIError | None = None):
        self.error = error

    async def ping(self):
        if self.error:
            raise self.error
        return True


async def test_admin_only_via_admin_emails(client, make_account, monkeypatch):
    await make_account("first@x.com")
    assert (await client.get("/api/auth/me")).json()["is_admin"] is False  # being first no longer matters
    assert (await client.get("/api/ai/status")).status_code == 200
    assert (await client.put("/api/admin/ai/token", json={"token": TOKEN})).status_code == 403
    await client.post("/api/auth/logout")
    monkeypatch.setattr(get_settings(), "admin_emails", "Helper@X.com, other@x.com")
    await make_account("helper@x.com")
    assert (await client.get("/api/auth/me")).json()["is_admin"] is True


async def test_admin_revoked_when_removed_from_admin_emails(client, make_account, monkeypatch):
    await make_account("owner@x.com", admin=True)
    assert (await client.get("/api/auth/me")).json()["is_admin"] is True
    monkeypatch.setattr(get_settings(), "admin_emails", "")
    assert (await client.get("/api/auth/me")).json()["is_admin"] is False
    assert (await client.post("/api/admin/ai/check")).status_code == 403


async def test_non_admin_cannot_manage_token(client, make_account):
    await make_account("user@x.com")
    assert (await client.put("/api/admin/ai/token", json={"token": TOKEN})).status_code == 403
    assert (await client.post("/api/admin/ai/check")).status_code == 403
    assert (await client.delete("/api/admin/ai/token")).status_code == 403
    assert credentials.read_token() is None


async def test_logged_out_is_rejected(client):
    for call in (client.get("/api/ai/status"), client.put("/api/admin/ai/token", json={"token": TOKEN})):
        assert (await call).status_code == 401


async def test_set_status_delete_flow_never_leaks_token(client, make_account):
    await make_account("owner@x.com", admin=True)
    st = (await client.get("/api/ai/status")).json()
    assert st == {"configured": False, "source": "none", "last_call": None}

    r = await client.put("/api/admin/ai/token", json={"token": f"  {TOKEN} "})
    assert r.status_code == 202 and r.json()["task_id"] > 0
    assert credentials.read_token() == TOKEN
    assert oct(os.stat(credentials.token_path()).st_mode & 0o777) == "0o600"

    st = await client.get("/api/ai/status")
    assert st.json()["configured"] is True and st.json()["source"] == "file"
    assert TOKEN not in st.text and TOKEN not in r.text

    assert (await client.delete("/api/admin/ai/token")).status_code == 204
    assert (await client.get("/api/ai/status")).json()["configured"] is False


async def test_invalid_token_is_rejected_without_saving(client, make_account):
    await make_account("owner@x.com", admin=True)
    r = await client.put("/api/admin/ai/token", json={"token": "nope"})
    assert r.status_code == 422 and credentials.read_token() is None
    assert TOKEN not in (await client.put("/api/admin/ai/token", json={"token": "bad token " + TOKEN})).text


async def test_status_reports_last_call(client, engine, make_account):
    await make_account("owner@x.com", admin=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        db.add(AICall(task="extract_profile", model="sonnet", duration_ms=5, ok=False, error_kind="auth"))
        await db.commit()
    last = (await client.get("/api/ai/status")).json()["last_call"]
    assert last["ok"] is False and last["error_kind"] == "auth" and last["at"]


async def test_check_task_reports_success_and_real_error(client, engine, make_account):
    await make_account("owner@x.com", admin=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    tid = (await client.post("/api/admin/ai/check")).json()["task_id"]
    assert (await client.post("/api/admin/ai/check")).json()["task_id"] == tid  # deduped while pending
    await run_task(tid, FakeAI(), maker)
    t = (await client.get(f"/api/tasks/{tid}")).json()
    assert t["status"] == "done" and t["result"] == {"ok": True}

    tid2 = (await client.post("/api/admin/ai/check")).json()["task_id"]
    await run_task(tid2, FakeAI(AIError("auth", "Not logged in")), maker)
    t = (await client.get(f"/api/tasks/{tid2}")).json()
    # an AI problem is a *result* the page can show, not a failed/retried task
    assert t["status"] == "done" and t["result"] == {"ok": False, "error_kind": "auth", "error": "Not logged in"}
