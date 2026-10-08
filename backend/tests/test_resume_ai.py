import json

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.ai.schemas import ResumeContact, TailoredResume
from jobfinder.ai.tasks import AITasks
from jobfinder.models import ProfileFact
from jobfinder.resumes import service
from jobfinder.storage.documents import DocumentStore
from tests.factories import make_match, make_user

RESUME = TailoredResume(
    contact=ResumeContact(name="Jane Doe"), summary="Backend engineer.", skills=["Go", "Kubernetes"],
    experience=[], projects=[], education=[],
)


class FakeClaude:
    def __init__(self, reply=RESUME):
        self.reply, self.prompts, self.systems, self.outputs = reply, [], [], []

    async def run(self, prompt, *, system, model="sonnet", timeout_s=180, task="", output=None):
        self.prompts.append(prompt)
        self.systems.append(system)
        self.outputs.append(output)
        return self.reply


async def seed(engine, tmp_path):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    store = DocumentStore(tmp_path / "store")
    async with maker() as db:
        user = await make_user(db)
        match, _ = await make_match(db, user.id)
        db.add(ProfileFact(user_id=user.id, question="Used Go?", answer="Yes at Stripe"))
        await db.commit()
    store.put(user.id, "resume", "cv.pdf", text="Built a Go retry service at Stripe", tags={"owner": "me"})
    return maker, store, user.id, match.id


async def test_prompt_has_profile_facts_documents_and_posting(engine, tmp_path):
    maker, store, uid, mid = await seed(engine, tmp_path)
    fake = FakeClaude()
    async with maker() as db:
        await service.tailor(db, AITasks(fake), store, uid, mid)
    p = fake.prompts[0]
    assert "Jane Doe" in p and "Used Go?" in p and "Yes at Stripe" in p
    assert "Built a Go retry service" in p and '<document name="cv.pdf">' in p
    assert "Backend Engineer" in p and "<job>" in p and "desc" in p
    assert "120000" not in p  # salary stays out
    assert fake.outputs[0] is TailoredResume
    assert "NEVER invent" in fake.systems[0] and "never invent experience, metrics or titles" in fake.systems[0]


async def test_stored_as_resume_tagged_with_match_and_replaced_on_retailor(engine, tmp_path):
    maker, store, uid, mid = await seed(engine, tmp_path)
    async with maker() as db:
        first = await service.tailor(db, AITasks(FakeClaude()), store, uid, mid)
        second = await service.tailor(db, AITasks(FakeClaude()), store, uid, mid)
    docs = store.list(uid, kind="resume", tags={"match": str(mid)})
    assert [d.id for d in docs] == [second["document_id"]] != [first["document_id"]]
    assert json.loads(store.read_text(uid, docs[0].id))["skills"] == ["Go", "Kubernetes"]
    assert len(store.list(uid, kind="resume")) == 2  # the uploaded resume is untouched


async def test_tailored_output_is_not_fed_back_as_a_source(engine, tmp_path):
    maker, store, uid, mid = await seed(engine, tmp_path)
    async with maker() as db:
        await service.tailor(db, AITasks(FakeClaude()), store, uid, mid)
    assert [label for label, _ in service.source_documents(store, uid)] == ["cv.pdf"]


async def test_unknown_match_and_missing_profile(engine, tmp_path):
    maker, store, uid, mid = await seed(engine, tmp_path)
    async with maker() as db:
        with pytest.raises(ValueError, match="Match not found"):
            await service.tailor(db, AITasks(FakeClaude()), store, uid, mid + 999)


def test_schema_rejects_missing_sections():
    data = RESUME.model_dump()
    for section in ("contact", "summary", "skills", "experience", "projects", "education"):
        with pytest.raises(ValidationError):
            TailoredResume.model_validate({k: v for k, v in data.items() if k != section})
