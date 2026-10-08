import json
from pathlib import Path

import httpx
import pytest

from jobfinder.ingest.sources import ATS_FETCHERS
from jobfinder.ingest.sources.smartrecruiters import fetch_smartrecruiters, parse_smartrecruiters
from jobfinder.ingest.sources.workable import fetch_workable, parse_workable

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def test_registered():
    assert "workable" in ATS_FETCHERS and "smartrecruiters" in ATS_FETCHERS


def test_workable_fixture():
    jobs = parse_workable(load("workable_sample.json"), "Example Co")
    assert [j.title for j in jobs] == ["Senior Backend Engineer", "Product Designer"]
    j = jobs[0]
    assert j.source == "workable" and j.external_id == "A1B2C3D4E5" and j.seniority == "senior"
    assert j.url == "https://apply.workable.com/j/A1B2C3D4E5"
    assert j.location == "Austin, Texas, United States"  # hidden location dropped
    assert j.workplace_type == "remote" and j.employment_type == "Full-time"
    assert "Build APIs in Python" in j.description_text and "<" not in j.description_text
    assert (j.salary_min, j.salary_max) == (150000, 190000)
    assert j.posted_at and j.posted_at.year == 2026
    d = jobs[1]
    assert d.location == "London, England, United Kingdom" and d.workplace_type is None and d.employment_type == "Contract"


async def test_fetch_workable_mock_transport():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/accounts/exco") and req.url.params["details"] == "true"
        return httpx.Response(200, json=load("workable_sample.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert len(await fetch_workable(c, "exco", "Example Co")) == 2


def test_smartrecruiters_fixture():
    details = list(load("smartrecruiters_sample.json")["details"].values())
    jobs = parse_smartrecruiters(details, "Example Co")
    assert len(jobs) == 2
    j = jobs[0]
    assert j.source == "smartrecruiters" and j.external_id == "743999000111222" and j.seniority == "senior"
    assert j.url.startswith("https://jobs.smartrecruiters.com/ExampleCo/743999000111222")
    assert j.location == "Dublin, Leinster, Ireland" and j.workplace_type == "remote"
    assert j.employment_type == "Full-time" and j.posted_at and j.posted_at.year == 2026
    assert "Build pipelines with Spark" in j.description_text and "Equal opportunity" in j.description_text
    assert "<" not in j.description_text and (j.salary_min, j.salary_max) == (140000, 170000)
    assert jobs[1].workplace_type == "hybrid"


async def test_fetch_smartrecruiters_mock_transport():
    fx = load("smartrecruiters_sample.json")

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/postings"):
            return httpx.Response(200, json=fx["list"])
        pid = path.rsplit("/", 1)[1]
        if pid == "743999000333444":
            return httpx.Response(500)  # detail failure must not drop the posting
        return httpx.Response(200, json=fx["details"][pid])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        jobs = await fetch_smartrecruiters(c, "ExampleCo", "Example Co")
    assert len(jobs) == 2
    assert "Spark" in jobs[0].description_text
    assert jobs[1].description_text == "" and jobs[1].url == "https://jobs.smartrecruiters.com/ExampleCo/743999000333444"


async def test_fetch_smartrecruiters_raises_when_every_detail_fails():
    fx = load("smartrecruiters_sample.json")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/postings"):
            return httpx.Response(200, json=fx["list"])
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(RuntimeError, match="detail requests failed"):
            await fetch_smartrecruiters(c, "ExampleCo", "Example Co")
