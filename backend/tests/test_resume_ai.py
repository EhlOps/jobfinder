import json

import pytest
from pydantic import ValidationError

from jobfinder.ai.tasks import AITasks
from jobfinder.core.errors import NotFound
from jobfinder.integrations.claude.schemas import ResumeContact, TailoredResume
from jobfinder.models import Profile, ProfileFact
from jobfinder.resumes import service
from jobfinder.services.matches import ProfileIncomplete
from jobfinder.storage.documents import DocumentStore
from tests.factories import make_match, make_user
from tests.fakes import FakeClaude

RESUME = TailoredResume(
    contact=ResumeContact(name="Jane Doe"), summary="Backend engineer.", skills=["Go", "Kubernetes"],
    experience=[], projects=[], education=[],
)


async def seed(maker, tmp_path):
    store = DocumentStore(tmp_path / "store")
    async with maker() as db:
        user = await make_user(db)
        match, _ = await make_match(db, user.id)
        db.add(ProfileFact(user_id=user.id, question="Used Go?", answer="Yes at Stripe"))
        await db.commit()
    store.put(user.id, "resume", "cv.pdf", text="Built a Go retry service at Stripe", tags={"owner": "me"})
    return maker, store, user.id, match.id


async def test_prompt_has_profile_facts_documents_and_posting(maker, tmp_path):
    maker, store, uid, mid = await seed(maker, tmp_path)
    fake = FakeClaude(default=RESUME)
    async with maker() as db:
        await service.tailor(db, AITasks(fake), store, uid, mid)
    p = fake.prompts[0]
    assert "Jane Doe" in p and "Used Go?" in p and "Yes at Stripe" in p
    assert "Built a Go retry service" in p and '<document name="cv.pdf">' in p
    assert "Backend Engineer" in p and "<job>" in p and "desc" in p
    assert "120000" not in p  # salary stays out
    assert fake.outputs[0] is TailoredResume
    assert "NEVER invent" in fake.systems[0] and "never invent experience, metrics or titles" in fake.systems[0]


async def test_stored_as_resume_tagged_with_match_and_replaced_on_retailor(maker, tmp_path):
    maker, store, uid, mid = await seed(maker, tmp_path)
    async with maker() as db:
        first = await service.tailor(db, AITasks(FakeClaude(default=RESUME)), store, uid, mid)
        second = await service.tailor(db, AITasks(FakeClaude(default=RESUME)), store, uid, mid)
    docs = store.list(uid, kind="resume", tags={"match": str(mid)})
    assert [d.id for d in docs] == [second["document_id"]] != [first["document_id"]]
    assert json.loads(store.read_text(uid, docs[0].id))["skills"] == ["Go", "Kubernetes"]
    assert len(store.list(uid, kind="resume")) == 2  # the uploaded resume is untouched


async def test_tailored_output_is_not_fed_back_as_a_source(maker, tmp_path):
    maker, store, uid, mid = await seed(maker, tmp_path)
    async with maker() as db:
        await service.tailor(db, AITasks(FakeClaude(default=RESUME)), store, uid, mid)
    assert [label for label, _ in service.source_documents(store, uid)] == ["cv.pdf"]


async def test_unknown_match_and_missing_profile(maker, tmp_path):
    maker, store, uid, mid = await seed(maker, tmp_path)
    async with maker() as db:
        with pytest.raises(NotFound, match="Match not found"):
            await service.tailor(db, AITasks(FakeClaude(default=RESUME)), store, uid, mid + 999)
        (await db.get(Profile, uid)).background = {}
        with pytest.raises(ProfileIncomplete, match="Finish your profile first"):
            await service.tailor(db, AITasks(FakeClaude(default=RESUME)), store, uid, mid)


def test_schema_rejects_missing_sections():
    data = RESUME.model_dump()
    for section in ("contact", "summary", "skills", "experience", "projects", "education"):
        with pytest.raises(ValidationError):
            TailoredResume.model_validate({k: v for k, v in data.items() if k != section})


async def test_edit_made_while_queued_survives_generation(maker, tmp_path):
    maker, store, uid, mid = await seed(maker, tmp_path)
    async with maker() as db:
        first = await service.tailor(db, AITasks(FakeClaude(default=RESUME)), store, uid, mid)
    store.set_tags(uid, first["document_id"], {**service.match_tags(mid), "edited": "1"})  # edited after enqueue
    async with maker() as db:
        with pytest.raises(ValueError, match="not regenerated"):
            await service.tailor(db, AITasks(FakeClaude(default=RESUME)), store, uid, mid)
    docs = store.list(uid, kind="resume", tags=service.match_tags(mid))
    assert [d.id for d in docs] == [first["document_id"]]


async def test_forced_generation_replaces_edited_resume(maker, tmp_path):
    maker, store, uid, mid = await seed(maker, tmp_path)
    async with maker() as db:
        first = await service.tailor(db, AITasks(FakeClaude(default=RESUME)), store, uid, mid)
        store.set_tags(uid, first["document_id"], {**service.match_tags(mid), "edited": "1"})
        second = await service.tailor(db, AITasks(FakeClaude(default=RESUME)), store, uid, mid, force=True)
    docs = store.list(uid, kind="resume", tags=service.match_tags(mid))
    assert [d.id for d in docs] == [second["document_id"]]


def test_edit_landing_between_check_and_delete_is_kept(tmp_path, monkeypatch):
    store = DocumentStore(tmp_path / "store")
    old, _ = store.put(1, "resume", "a.json", text="{}", content_type="application/json", tags=service.match_tags(5))

    def edit_now(*args):  # the user's edit lands right after the pre-save check
        store.set_tags(1, old.id, {**service.match_tags(5), "edited": "1"})
        return False

    monkeypatch.setattr(service, "_edited", edit_now)
    service.save(store, 1, 5, RESUME)
    assert old.id in [m.id for m in store.list(1, kind="resume")]
