import io

import docx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.ai.claude_code import AIError
from jobfinder.ai.schemas import Experience, ResumeContact, TailoredResume
from jobfinder.models import Job, Task
from jobfinder.worker import run_task
from tests.test_cover_letters_api import setup as _setup

BASE = "/api/matches/{}/resume"


def _resume(skills):
    return TailoredResume(
        contact=ResumeContact(name="Jane Doe", email="jane@example.com"), summary="Backend engineer.", skills=skills,
        experience=[Experience(company="Stripe", title="Engineer", bullets=["Built a Go retry service"])],
        projects=[], education=[],
    )


class FakeAI:
    def __init__(self, resume=None, error=None):
        self.resume, self.error, self.calls = resume or _resume(["Go"]), error, []

    async def tailor_resume(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.resume


async def setup(client, engine):
    mid, job_id = await _setup(client, engine)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        job = await db.get(Job, job_id)
        job.description_text = "We use Go, Kubernetes and Terraform every day."
        await db.commit()
    return mid


async def generate(client, engine, mid, ai=None, **body):
    r = await client.post(BASE.format(mid), json=body)
    assert r.status_code == 202, r.text
    await run_task(r.json()["task_id"], ai or FakeAI(), async_sessionmaker(engine, expire_on_commit=False))
    return r.json()["task_id"]


async def test_generate_edit_download_flow(authed, engine):
    mid = await setup(authed, engine)
    url = BASE.format(mid)
    assert (await authed.get(url)).json() == {"resume": None, "pending_task_id": None}

    ai = FakeAI()
    tid = await generate(authed, engine, mid, ai)
    assert (await authed.get(f"/api/tasks/{tid}")).json()["status"] == "done"
    assert ai.calls[0]["job"]["company"] == "Acme/Co" and "salary" not in ai.calls[0]["job"]

    state = (await authed.get(url)).json()
    r = state["resume"]
    assert r["edited"] is False and r["content"]["skills"] == ["Go"]
    cov = r["coverage"]
    assert cov["ats"] == "greenhouse"  # read from the job's source
    assert "Go" in cov["covered"] and "Kubernetes" in cov["missing"] and cov["notes"]
    before = cov["percent"]

    content = r["content"]
    content["skills"] = ["Go", "Kubernetes", "Terraform"]
    put = await authed.put(url, json={"content": content})
    assert put.status_code == 200
    out = put.json()
    assert out["edited"] is True and out["coverage"]["percent"] > before and "Terraform" in out["coverage"]["covered"]
    again = (await authed.get(url)).json()["resume"]  # persisted and recomputed from the saved text
    assert again["edited"] is True and again["coverage"] == out["coverage"]
    assert again["content"]["skills"] == ["Go", "Kubernetes", "Terraform"]

    d = await authed.get(url + "/download?format=docx")
    assert d.status_code == 200 and d.content[:2] == b"PK" and "Resume" in d.headers["content-disposition"]
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(d.content)).paragraphs)
    assert "Terraform" in text
    p = await authed.get(url + "/download?format=pdf")
    assert p.status_code == 200 and p.content[:5] == b"%PDF-" and p.headers["content-type"] == "application/pdf"
    assert (await authed.get(url + "/download")).content[:2] == b"PK"  # docx by default
    assert (await authed.get(url + "/download?format=txt")).status_code == 422

    assert (await authed.delete(url)).status_code == 204
    assert (await authed.get(url)).json()["resume"] is None
    assert (await authed.get(url + "/download")).status_code == 404


async def test_edited_resume_needs_force_to_regenerate(authed, engine):
    mid = await setup(authed, engine)
    url = BASE.format(mid)
    await generate(authed, engine, mid)
    content = (await authed.get(url)).json()["resume"]["content"]
    content["summary"] = "My own words."
    await authed.put(url, json={"content": content})
    assert (await authed.post(url, json={})).status_code == 409

    await generate(authed, engine, mid, FakeAI(_resume(["Rust"])), force=True)
    r = (await authed.get(url)).json()["resume"]
    assert r["edited"] is False and r["content"]["skills"] == ["Rust"]


async def test_pending_task_and_dedupe(authed, engine):
    mid = await setup(authed, engine)
    url = BASE.format(mid)
    a = (await authed.post(url, json={})).json()["task_id"]
    assert (await authed.post(url, json={})).json()["task_id"] == a
    assert (await authed.get(url)).json()["pending_task_id"] == a


async def test_edit_needs_a_resume_and_generate_needs_a_profile(authed, engine):
    mid, _ = await _setup(authed, engine, background=False)
    url = BASE.format(mid)
    assert (await authed.post(url, json={})).status_code == 422
    assert (await authed.put(url, json={"content": _resume(["Go"]).model_dump()})).status_code == 404
    assert (await authed.put(url, json={"content": {"summary": "x"}})).status_code == 422


async def test_claude_failure_fails_task_and_keeps_resume(authed, engine):
    mid = await setup(authed, engine)
    url = BASE.format(mid)
    await generate(authed, engine, mid)
    r = await authed.post(url, json={"force": True})
    ai = FakeAI(error=AIError("auth", "Claude is not connected"))
    await run_task(r.json()["task_id"], ai, async_sessionmaker(engine, expire_on_commit=False))
    async with async_sessionmaker(engine)() as db:
        status = await db.scalar(sa.select(Task.status).where(Task.id == r.json()["task_id"]))
    assert status in ("failed", "queued")
    assert (await authed.get(url)).json()["resume"]["content"]["skills"] == ["Go"]  # old resume survives


async def test_resumes_are_private_to_their_owner(authed, client, engine, make_account):
    mid = await setup(authed, engine)
    await generate(authed, engine, mid)
    await client.post("/api/auth/logout")
    await make_account("other@x.com", "password-1234")
    base = BASE.format(mid)
    body = {"content": _resume(["Go"]).model_dump()}
    assert (await client.get(base)).status_code == 404
    assert (await client.post(base, json={})).status_code == 404
    assert (await client.put(base, json=body)).status_code == 404
    assert (await client.delete(base)).status_code == 404
    assert (await client.get(base + "/download")).status_code == 404


async def test_requires_login(client):
    assert (await client.get("/api/matches/1/resume")).status_code == 401
