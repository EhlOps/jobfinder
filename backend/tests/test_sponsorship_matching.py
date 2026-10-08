import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.ai.schemas import MatchScore
from jobfinder.ai.tasks import AITasks, load_prompt
from jobfinder.api.profile import derive_needs_sponsorship
from jobfinder.matching.service import sponsorship_info
from jobfinder.models import Company, Job, JobMatch
from tests.test_matches_api import add_match, me


class FakeClaude:
    def __init__(self):
        self.prompts = []

    async def run(self, prompt, *, system, output=None, model=None, task="", timeout_s=180):
        self.prompts.append(prompt)
        return MatchScore()


def job(desc="About the role"):
    return Job(source="greenhouse", external_id="1", company_name="Acme", title="Engineer", url="https://x/1",
               location="Boston", description_text=desc, dedupe_hash="h")


@pytest.mark.parametrize("auth,expected", [
    ("citizen", False), ("permanent_resident", False), ("f1_opt", True), ("stem_opt", True),
    ("h1b_transfer", True), ("other", None), (None, None),
])
def test_derives_needs_sponsorship(auth, expected):
    assert derive_needs_sponsorship(auth) is expected


def test_explicit_answer_wins_over_derivation():
    assert derive_needs_sponsorship("f1_opt", False) is False
    assert derive_needs_sponsorship("citizen", True) is True


async def test_profile_derives_when_unset_and_bumps_version(authed):
    r = await authed.put("/api/profile/status", json={"work_authorization": "stem_opt"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"]["work_authorization"] == "stem_opt" and body["status"]["needs_visa_sponsorship"] is True
    r2 = await authed.put("/api/profile/status", json={"work_authorization": "citizen"})
    assert r2.json()["version"] == body["version"] + 1 and r2.json()["status"]["needs_visa_sponsorship"] is False
    r3 = await authed.put("/api/profile/status", json={"work_authorization": "f1_opt", "needs_visa_sponsorship": False})
    assert r3.json()["status"]["needs_visa_sponsorship"] is False and r3.json()["version"] == body["version"] + 2
    assert (await authed.put("/api/profile/status", json={"work_authorization": "bogus"})).status_code == 422
    assert (await authed.get("/api/profile")).json()["status"]["work_authorization"] == "f1_opt"


def test_sponsorship_info_only_when_needed():
    assert sponsorship_info({"needs_visa_sponsorship": False, "work_authorization": "citizen"}, job(), None) is None
    assert sponsorship_info({}, job("We sponsor visas"), None) is None
    info = sponsorship_info(
        {"needs_visa_sponsorship": True, "work_authorization": "f1_opt"},
        job("We do not sponsor visas."), Company(name="Acme", ats="x", slug="a", sponsors_visas=True),
    )
    assert info == {"needs_sponsorship": True, "work_authorization": "f1_opt", "posting_says": "refuses", "company_sponsors": True}
    # unknowns are left out so silence does not read as a refusal
    assert sponsorship_info({"needs_visa_sponsorship": True}, job(), None) == {"needs_sponsorship": True}


async def test_candidate_data_has_sponsorship_block_only_when_relevant():
    fake = FakeClaude()
    ai = AITasks(fake)
    args = ({"target_roles": ["x"]}, {}, [], {"title": "Engineer", "description": "d"})
    await ai.score_match(*args)
    await ai.score_match(*args, sponsorship={"needs_sponsorship": True, "posting_says": "refuses"})
    assert "sponsorship" not in fake.prompts[0].split("<job>")[0]
    cand = fake.prompts[1].split("<job>")[0]
    assert '"sponsorship"' in cand and '"posting_says": "refuses"' in cand
    assert "`sponsorship` block" in load_prompt("match")


async def test_api_exposes_sponsorship_value(authed, engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    uid = await me(authed)
    plain = await add_match(maker, uid, "Plain Role", 50)
    refuses = await add_match(maker, uid, "Refuses Role", 60)
    friendly = await add_match(maker, uid, "Sponsors Role", 70)
    flagged = await add_match(maker, uid, "Company Role", 80)
    async with maker() as db:
        c = Company(name="Acme", ats="greenhouse", slug="acme", sponsors_visas=True)
        d = Company(name="Nope", ats="greenhouse", slug="nope", sponsors_visas=False)
        db.add_all([c, d])
        await db.flush()
        for mid, desc, comp in [(refuses, "We cannot sponsor visas.", d), (friendly, "We will sponsor H-1B visas.", None), (flagged, "Great team.", c)]:
            m = await db.get(JobMatch, mid)
            j = await db.get(Job, m.job_id)
            j.description_text = desc
            j.company_id = comp.id if comp else None
        await db.commit()
    items = {i["job"]["title"]: i["job"]["sponsorship"] for i in (await authed.get("/api/matches")).json()["items"]}
    assert items == {"Plain Role": None, "Refuses Role": "refuses", "Sponsors Role": "sponsors", "Company Role": "likely"}
    assert (await authed.get(f"/api/matches/{flagged}")).json()["job"]["sponsorship"] == "likely"
    assert (await authed.get(f"/api/matches/{plain}")).json()["job"]["sponsorship"] is None
