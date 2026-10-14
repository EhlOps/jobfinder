import httpx
import pytest

from jobfinder.ingest import http_safety
from jobfinder.ingest.base import JobPosting
from jobfinder.ingest.recipes import (
    Recipe,
    RecipeError,
    parse_recipe,
    recipe_finds,
    run_recipe,
    validate_recipe,
)
from jobfinder.ingest.recipes.json_api import compile_path, get_all, parse_date

PAGES = {
    "0": {"data": {"total": 3, "jobs": [
        {"id": 101, "title": "Robotics Engineer", "loc": {"name": "Boston, MA"}, "posted": "2026-10-01",
         "body": "<p>Build robots. $120,000 - $150,000</p>"},
        {"id": 102, "title": "Controls Engineer", "loc": {"name": "Remote"}, "posted": 1791939191},
    ]}},
    "2": {"data": {"jobs": [{"id": 103, "title": "Test Engineer", "loc": None, "posted": None}]}},
    "4": {"data": {"jobs": []}},
}


def recipe(**over):
    base = {
        "kind": "json_api",
        "list": {"url": "https://careers.acme.com/api/jobs?size=2"},
        "pagination": {"kind": "offset", "param": "from", "start": 0, "step": 2, "max_pages": 10},
        "items": "$.data.jobs[*]",
        "fields": {"external_id": "$.id", "title": "$.title", "url": "https://careers.acme.com/jobs/{external_id}",
                   "location": "$.loc.name", "posted_at": "$.posted", "description": "$.body"},
    }
    base.update(over)
    return base


@pytest.fixture
def public_dns(monkeypatch):
    async def fake(url):
        return httpx.URL(url).host, "93.184.216.34"

    monkeypatch.setattr(http_safety, "resolve_public", fake)


def api_client(calls):
    def handler(request):
        calls.append(str(request.url.params.get("from")))
        return httpx.Response(200, json=PAGES.get(request.url.params.get("from"), {"data": {"jobs": []}}))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_path_subset():
    data = {"a": {"b": [{"c": 1}, {"c": 2}]}, "k-1": {"x y": 3}}
    assert get_all(data, "$.a.b[*].c") == [1, 2]
    assert get_all(data, "$.a.b[1].c") == [2]
    assert get_all(data, "$['k-1'][\"x y\"]") == [3]
    assert get_all(data, "$.missing.c") == []
    assert compile_path("$.a[*]") == ["a", "*"]
    for bad in ("a.b", "$..a", "$.a[?(@.x)]", "$.a.__class__()", "$.a;import os"):
        with pytest.raises(RecipeError):
            compile_path(bad)


def test_parse_date():
    assert parse_date("2026-10-01").year == 2026
    assert parse_date(1791939191).year == 2026 and parse_date(1791939191000).year == 2026
    assert parse_date("2026-10-01T10:00:00Z").tzinfo is not None
    assert parse_date("not a date") is None and parse_date(None) is None


async def test_json_api_runner_pages_and_maps(public_dns):
    calls = []
    async with api_client(calls) as c:
        jobs = await run_recipe(c, "acme.com", "Acme", recipe())
    assert calls == ["0", "2", "4"]  # stops at the empty page
    assert [j.title for j in jobs] == ["Robotics Engineer", "Controls Engineer", "Test Engineer"]
    first = jobs[0]
    assert first.source == "recipe" and first.external_id == "acme.com:101"
    assert first.url == "https://careers.acme.com/jobs/101" and first.location == "Boston, MA"
    assert first.salary_min == 120000 and "Build robots" in first.description_text and "<p>" not in first.description_text
    assert jobs[1].posted_at.year == 2026 and jobs[2].location == ""


async def test_post_pagination_in_body(public_dns):
    bodies = []

    def handler(request):
        import json
        body = json.loads(request.content)
        bodies.append(body["from"])
        return httpx.Response(200, json={"data": {"jobs": [{"id": body["from"], "title": "T"}] if body["from"] < 2 else []}})

    r = recipe(list={"method": "POST", "url": "https://acme.com/widgets", "json_body": {"from": 0, "size": 1}},
               pagination={"kind": "offset", "param": "from", "step": 1, "max_pages": 5},
               fields={"external_id": "$.id", "title": "$.title", "url": "/jobs/{external_id}"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        jobs = await run_recipe(c, "acme.com", "Acme", r)
    assert bodies == [0, 1, 2] and [j.url for j in jobs] == ["https://acme.com/jobs/0", "https://acme.com/jobs/1"]


@pytest.mark.parametrize("bad", [
    {"list": {"url": "javascript:alert(1)"}},
    {"list": {"url": "file:///etc/passwd"}},
    {"list": {"url": "https://u:p@acme.com/api"}},
    {"list": {"url": "https://acme.com/api", "headers": {"Cookie": "session=1"}}},
    {"list": {"url": "https://acme.com/api", "headers": {"Authorization": "Bearer x"}}},
    {"list": {"url": "https://acme.com/api", "headers": {"accept": "a\r\nX-Evil: 1"}}},
    {"kind": "python"},
    {"pagination": {"kind": "page", "max_pages": 10_000}},
])
def test_recipe_schema_refuses_unsafe(bad):
    with pytest.raises(RecipeError):
        parse_recipe(recipe(**bad))


async def test_recipe_requests_are_ssrf_checked():
    r = recipe(list={"url": "http://169.254.169.254/latest/meta-data/"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={}))) as c:
        with pytest.raises(http_safety.FetchError):
            await run_recipe(c, "acme.com", "Acme", r)


async def test_unknown_kind_and_empty_recipe():
    with pytest.raises(RecipeError, match="no recipe"):
        await run_recipe(None, "acme.com", "Acme", None)
    with pytest.raises(RecipeError, match="no runner"):
        await run_recipe(None, "acme.com", "Acme", Recipe.model_validate(recipe(kind="rendered_html")))


def _p(eid, url):
    return JobPosting(source="recipe", external_id=f"acme.com:{eid}", company_name="Acme", title="T", url=url)


def test_validate_recipe_requires_the_pasted_job():
    jobs = [_p("4021", "https://careers.acme.com/jobs/4021"), _p("4022", "https://careers.acme.com/jobs/4022")]
    assert validate_recipe(jobs, "http://www.careers.acme.com/jobs/4021/?utm=x")[0]
    assert recipe_finds(jobs, "https://acme.com/apply?jobId=4022")  # id as a whole token
    ok, why = validate_recipe(jobs, "https://careers.acme.com/jobs/9999")
    assert not ok and "pasted job" in why
    assert not validate_recipe([], None)[0]
    assert not recipe_finds([_p("40", "https://x/40")], "https://acme.com/jobs/140")  # short ids need exact URL
