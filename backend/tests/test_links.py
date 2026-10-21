import pytest

from jobfinder.models import Task
from jobfinder.profile import github, web_page


def test_parse_github_username():
    assert github.parse_username("https://github.com/torvalds") == "torvalds"
    assert github.parse_username("github.com/torvalds/linux") == "torvalds"
    with pytest.raises(github.GitHubError):
        github.parse_username("https://example.com/foo")


def test_summarize_repos_skips_forks_and_ranks_by_stars():
    repos = [
        {"name": "big", "language": "Rust", "stargazers_count": 50, "description": "d", "topics": ["cli"]},
        {"name": "forked", "fork": True, "language": "Go", "stargazers_count": 999},
        {"name": "small", "language": "Python", "stargazers_count": 1},
    ]
    out = github.summarize_repos("me", repos, {"big": "# Big\n\nA   readme"})
    assert "Original repos: 2" in out and "forked" not in out
    assert out.index("big") < out.index("small")
    assert "README: # Big A readme" in out


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/", "http://localhost:8000", "http://169.254.169.254/latest", "ftp://x.com"]
)
async def test_ssrf_guard_blocks_internal(url):
    with pytest.raises(web_page.FetchError):
        await web_page.assert_public_url(url)


async def _run(maker, task_id):
    from tests.test_documents import _run as run

    return await run(maker, task_id)


async def _add(authed, maker, kind, url) -> dict:
    """Add a link, let the worker fetch it, and return it as the list then shows it."""
    r = await authed.post("/api/links", json={"kind": kind, "url": url})
    assert r.status_code == 202, r.text
    if r.json()["task_id"]:
        await _run(maker, r.json()["task_id"])
    return next(x for x in (await authed.get("/api/links")).json() if x["id"] == r.json()["link"]["id"])


async def test_link_crud_and_fetch_error_recorded(authed, maker, monkeypatch):
    async def boom(url):
        raise web_page.FetchError("HTTP 500")

    monkeypatch.setattr(web_page, "fetch_page_text", boom)
    r = await authed.post("/api/links", json={"kind": "portfolio", "url": "https://me.dev"})
    assert r.status_code == 202
    link = r.json()["link"]
    assert link["status"] == "processing" and not link["fetched"] and link["fetch_error"] is None
    assert link["id"].startswith("doc_") and link["kind"] == "portfolio"
    async with maker() as db:
        task = await db.get(Task, r.json()["task_id"])
    assert (task.kind, task.queue, task.payload) == ("fetch_link", "io", {"doc_id": link["id"]})
    assert (await _run(maker, task.id)).status == "done"
    link = (await authed.get("/api/links")).json()[0]
    assert link["status"] == "error" and link["fetch_error"] == "HTTP 500" and not link["fetched"]

    r = await authed.post("/api/links", json={"kind": "linkedin", "url": "https://linkedin.com/in/me"})
    assert r.status_code == 202 and r.json()["task_id"] is None  # saved, never fetched
    assert r.json()["link"]["status"] == "ready" and r.json()["link"]["fetch_error"] is None
    assert not r.json()["link"]["fetched"]

    assert (await authed.post("/api/links", json={"kind": "other", "url": "nope"})).status_code == 422
    assert len((await authed.get("/api/links")).json()) == 2
    assert (await authed.get("/api/documents")).json() == []  # links don't show up as documents


async def test_link_fetch_success_refresh_and_tags(authed, maker, monkeypatch):
    texts = iter(["first summary", "second summary"])

    async def ok(url):
        return next(texts)

    monkeypatch.setattr(github, "fetch_github_summary", ok)
    link = await _add(authed, maker, "github", "https://github.com/me")
    assert link["fetched"] is True and link["status"] == "ready" and link["url"] == "https://github.com/me"
    link_id = link["id"]
    assert (await authed.get(f"/api/documents/{link_id}/text")).text == "first summary"
    meta = (await authed.get(f"/api/documents/{link_id}")).json()
    assert meta["tags"]["link_kind"] == "github" and meta["tags"]["source"] == "link"

    r = await authed.post(f"/api/links/{link_id}/refresh")
    assert r.status_code == 202 and r.json()["link"]["status"] == "processing"
    assert (await authed.get(f"/api/documents/{link_id}/text")).text == "first summary"  # until the new fetch lands
    await _run(maker, r.json()["task_id"])
    assert (await authed.get(f"/api/documents/{link_id}/text")).text == "second summary"
    assert (await authed.get("/api/links")).json()[0]["status"] == "ready"
    assert (await authed.delete(f"/api/links/{link_id}")).status_code == 204
    assert (await authed.get("/api/links")).json() == []


async def test_the_legacy_worker_fetches_links(authed, maker, monkeypatch):
    from jobfinder.worker import run_task

    async def ok(url):
        return "my portfolio"

    monkeypatch.setattr(web_page, "fetch_page_text", ok)
    r = await authed.post("/api/links", json={"kind": "portfolio", "url": "https://me.dev"})
    await run_task(r.json()["task_id"], None, maker)
    assert (await authed.get("/api/links")).json()[0]["fetched"] is True


async def test_a_link_deleted_before_its_fetch_is_skipped(authed, maker):
    r = (await authed.post("/api/links", json={"kind": "portfolio", "url": "https://me.dev"})).json()
    assert (await authed.delete(f"/api/links/{r['link']['id']}")).status_code == 204
    assert (await _run(maker, r["task_id"])).result["skipped"] == "deleted"


async def test_the_worker_fetch_keeps_the_ssrf_guard(authed, maker):
    """The real fetcher runs in the worker: an internal address is refused there and recorded."""
    link = await _add(authed, maker, "portfolio", "http://127.0.0.1:9/")
    assert link["status"] == "error" and "non-public" in link["fetch_error"]


async def test_documents_cannot_be_managed_through_link_endpoints(authed):
    doc = (await authed.post("/api/documents", files={"file": ("a.txt", b"hi", "text/plain")})).json()["document"]
    assert (await authed.post(f"/api/links/{doc['id']}/refresh")).status_code == 404
    assert (await authed.delete(f"/api/links/{doc['id']}")).status_code == 404
    assert (await authed.get(f"/api/documents/{doc['id']}")).status_code == 200
