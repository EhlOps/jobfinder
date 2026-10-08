import json
from pathlib import Path

import httpx

from jobfinder.ingest.sources import ATS_FETCHERS
from jobfinder.ingest.sources.bamboohr import fetch_bamboohr, parse_bamboohr
from jobfinder.ingest.sources.recruitee import fetch_recruitee, parse_recruitee

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def test_registered():
    assert ATS_FETCHERS["recruitee"] is fetch_recruitee and ATS_FETCHERS["bamboohr"] is fetch_bamboohr


def test_recruitee_fixture():
    jobs = parse_recruitee(load("recruitee_sample.json"), "bunq")
    assert len(jobs) == 4 and all(j.source == "recruitee" and j.company_name == "bunq" for j in jobs)
    j = jobs[0]
    assert j.title == "Deputy AML Manager - Belgium" and j.seniority == "manager"
    assert j.location == "Brussels, Brussels, Belgium" and j.workplace_type == "hybrid"
    assert j.url == "https://careers.bunq.com/o/deputy-aml-manager-belgium" and j.employment_type == "fulltime_permanent"
    assert j.posted_at and j.posted_at.year == 2026 and j.posted_at.tzinfo is not None
    assert "<p>" not in j.description_text and len(j.description_text) > 200
    remote = next(j for j in jobs if j.title.endswith("(Remote)"))
    assert remote.workplace_type == "remote"
    assert (remote.salary_min, remote.salary_max, remote.salary_currency) == (90000, 120000, "EUR")


def test_recruitee_skips_unpublished_and_untitled():
    payload = {"offers": [
        {"id": 1, "title": "Draft", "status": "draft"},
        {"id": 2, "title": " ", "status": "published"},
        {"id": 3, "title": "Real", "status": "published", "careers_url": "javascript:alert(1)"},
    ]}
    jobs = parse_recruitee(payload, "X")
    assert [j.title for j in jobs] == ["Real"] and jobs[0].url == ""


def test_bamboohr_fixture():
    data = load("bamboohr_sample.json")
    jobs = parse_bamboohr(data["list"], data["details"], "Acme", "acme")
    assert [j.external_id for j in jobs] == ["41", "42", "43"]
    sr = jobs[0]
    assert sr.title == "Senior Software Engineer" and sr.seniority == "senior"
    assert sr.location == "Austin, Texas" and sr.workplace_type == "hybrid" and sr.employment_type == "Full-Time"
    assert sr.url == "https://acme.bamboohr.com/careers/41" and sr.posted_at.isoformat().startswith("2026-09-30")
    assert "Design APIs" in sr.description_text and "<li>" not in sr.description_text
    assert (sr.salary_min, sr.salary_max) == (140000, 170000)
    assert jobs[1].workplace_type == "remote" and jobs[1].location == "Remote"
    assert jobs[2].workplace_type == "onsite" and jobs[2].posted_at is None


def test_fetchers_over_mock_transport():
    data = load("bamboohr_sample.json")

    def handler(req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        if host == "acme.bamboohr.com" and path == "/careers/list":
            return httpx.Response(200, json=data["list"])
        if host == "acme.bamboohr.com" and path.endswith("/detail"):
            jid = path.split("/")[2]
            if jid == "43":
                return httpx.Response(500)  # a failed detail call must not drop the job
            return httpx.Response(200, json=data["details"][jid])
        if host == "bunq.recruitee.com" and path == "/api/offers/":
            return httpx.Response(200, json=load("recruitee_sample.json"))
        return httpx.Response(404)

    import asyncio

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return (
                await fetch_bamboohr(client, "acme", "Acme"),
                await fetch_recruitee(client, "bunq", "bunq"),
            )

    bamboo, rec = asyncio.run(run())
    assert len(bamboo) == 3 and bamboo[2].description_text == "" and "Design APIs" in bamboo[0].description_text
    assert len(rec) == 4
