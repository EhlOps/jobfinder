import pytest

from jobfinder.integrations.claude.claude_code import AIError
from tests.fakes import FakeOnboardingAI, run_task


async def upload(authed, maker, name: str, data: bytes) -> None:
    """Upload a file and let the worker read it (parse_document, #51)."""
    r = await authed.post("/api/documents", files={"file": (name, data, "text/plain")})
    await run_task(r.json()["task_id"], None, maker)


async def test_extract_requires_something_to_read(authed):
    r = await authed.post("/api/onboarding/extract")
    assert r.status_code == 422


async def test_extract_flow(authed, maker):
    await upload(authed, maker, "resume.txt", b"Intern at Acme")
    r = await authed.post("/api/onboarding/extract")
    assert r.status_code == 202
    tid = r.json()["task_id"]
    assert (await authed.post("/api/onboarding/extract")).json()["task_id"] == tid  # deduped
    assert (await authed.get(f"/api/tasks/{tid}")).json()["status"] == "queued"

    ai = FakeOnboardingAI()
    await run_task(tid, ai, maker)
    t = (await authed.get(f"/api/tasks/{tid}")).json()
    assert t["status"] == "done" and t["result"]["experience"][0]["company"] == "Acme"
    assert ai.extract_sources == [("resume.txt", "Intern at Acme")]


async def test_extract_ignores_tailored_resumes(authed, maker):
    from jobfinder.services.resumes import match_tags
    from jobfinder.storage.documents import get_store

    await upload(authed, maker, "resume.txt", b"Intern at Acme")
    uid = (await authed.get("/api/auth/me")).json()["id"]
    get_store().put(uid, "resume", "Tailored resume - x.json", text='{"summary": "reworded"}',
                    content_type="application/json", tags=match_tags(7))
    tid = (await authed.post("/api/onboarding/extract")).json()["task_id"]
    ai = FakeOnboardingAI()
    await run_task(tid, ai, maker)
    assert ai.extract_sources == [("resume.txt", "Intern at Acme")]


async def test_followups_flow(authed, maker):
    assert (await authed.post("/api/onboarding/followups")).status_code == 422
    await authed.put("/api/profile/background", json={"summary": "SWE"})
    await authed.post("/api/profile/facts", json={"question": "q1", "answer": "a1"})
    tid = (await authed.post("/api/onboarding/followups")).json()["task_id"]

    ai = FakeOnboardingAI()
    await run_task(tid, ai, maker)
    t = (await authed.get(f"/api/tasks/{tid}")).json()
    assert t["status"] == "done" and t["result"]["questions"][0]["question"].startswith("Have you used")
    assert ai.seen[1] == {"summary": "SWE"} and ai.seen[2] == [("q1", "a1")]


@pytest.mark.parametrize(("exc", "status"), [(AIError("rate_limited", "limit"), "queued"), (AIError("auth", "login"), "failed")])
async def test_ai_errors_retry_only_when_retryable(authed, maker, exc, status):
    await upload(authed, maker, "r.txt", b"x")
    tid = (await authed.post("/api/onboarding/extract")).json()["task_id"]
    ai = FakeOnboardingAI()
    ai.fail_with = exc
    await run_task(tid, ai, maker)
    t = (await authed.get(f"/api/tasks/{tid}")).json()
    assert t["status"] == status and exc.kind in t["error"]


async def test_tasks_are_private(authed, client, make_account, maker):
    await upload(authed, maker, "r.txt", b"x")
    tid = (await authed.post("/api/onboarding/extract")).json()["task_id"]
    await client.post("/api/auth/logout")
    await make_account("z@x.com", "password-zzzz")
    assert (await client.get(f"/api/tasks/{tid}")).status_code == 404
