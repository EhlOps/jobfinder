import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.ai.claude_code import AIError
from jobfinder.ai.schemas import Background, Experience, FollowupQuestion
from jobfinder.worker import run_task


class FakeAI:
    def __init__(self):
        self.extract_sources = None
        self.fail_with: Exception | None = None

    async def extract_profile(self, sources):
        if self.fail_with:
            raise self.fail_with
        self.extract_sources = sources
        return Background(summary="SWE", experience=[Experience(company="Acme", title="Intern", kind="internship")])

    async def followup_questions(self, status, background, facts):
        self.seen = (status, background, facts)
        return [FollowupQuestion(question="Have you used Kubernetes?", why="Many roles ask for it")]


async def test_extract_requires_something_to_read(authed):
    r = await authed.post("/api/onboarding/extract")
    assert r.status_code == 422


async def test_extract_flow(authed, engine):
    await authed.post("/api/documents", files={"file": ("resume.txt", b"Intern at Acme", "text/plain")})
    r = await authed.post("/api/onboarding/extract")
    assert r.status_code == 202
    tid = r.json()["task_id"]
    assert (await authed.post("/api/onboarding/extract")).json()["task_id"] == tid  # deduped
    assert (await authed.get(f"/api/tasks/{tid}")).json()["status"] == "queued"

    ai = FakeAI()
    await run_task(tid, ai, async_sessionmaker(engine, expire_on_commit=False))
    # not claimed by the worker loop in this test, so run_task's handler still works on a queued row
    t = (await authed.get(f"/api/tasks/{tid}")).json()
    assert t["status"] == "done" and t["result"]["experience"][0]["company"] == "Acme"
    assert ai.extract_sources == [("resume.txt", "Intern at Acme")]


async def test_followups_flow(authed, engine):
    assert (await authed.post("/api/onboarding/followups")).status_code == 422
    await authed.put("/api/profile/background", json={"summary": "SWE"})
    await authed.post("/api/profile/facts", json={"question": "q1", "answer": "a1"})
    tid = (await authed.post("/api/onboarding/followups")).json()["task_id"]

    ai = FakeAI()
    await run_task(tid, ai, async_sessionmaker(engine, expire_on_commit=False))
    t = (await authed.get(f"/api/tasks/{tid}")).json()
    assert t["status"] == "done" and t["result"]["questions"][0]["question"].startswith("Have you used")
    assert ai.seen[1] == {"summary": "SWE"} and ai.seen[2] == [("q1", "a1")]


@pytest.mark.parametrize(("exc", "status"), [(AIError("rate_limited", "limit"), "queued"), (AIError("auth", "login"), "failed")])
async def test_ai_errors_retry_only_when_retryable(authed, engine, exc, status):
    await authed.post("/api/documents", files={"file": ("r.txt", b"x", "text/plain")})
    tid = (await authed.post("/api/onboarding/extract")).json()["task_id"]
    ai = FakeAI()
    ai.fail_with = exc
    await run_task(tid, ai, async_sessionmaker(engine, expire_on_commit=False))
    t = (await authed.get(f"/api/tasks/{tid}")).json()
    assert t["status"] == status and exc.kind in t["error"]


async def test_tasks_are_private(authed, client, make_account):
    await authed.post("/api/documents", files={"file": ("r.txt", b"x", "text/plain")})
    tid = (await authed.post("/api/onboarding/extract")).json()["task_id"]
    await client.post("/api/auth/logout")
    await make_account("z@x.com", "password-zzzz")
    assert (await client.get(f"/api/tasks/{tid}")).status_code == 404
