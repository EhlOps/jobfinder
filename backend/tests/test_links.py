import pytest

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


async def test_link_crud_and_fetch_error_recorded(authed, monkeypatch):
    async def boom(url):
        raise web_page.FetchError("HTTP 500")

    from jobfinder.api import links

    monkeypatch.setattr(links, "fetch_page_text", boom)
    r = await authed.post("/api/links", json={"kind": "portfolio", "url": "https://me.dev"})
    assert r.status_code == 201 and r.json()["fetch_error"] == "HTTP 500" and not r.json()["fetched"]
    assert r.json()["id"].startswith("doc_") and r.json()["kind"] == "portfolio"

    r = await authed.post("/api/links", json={"kind": "linkedin", "url": "https://linkedin.com/in/me"})
    assert r.json()["fetch_error"] is None and not r.json()["fetched"]

    assert (await authed.post("/api/links", json={"kind": "other", "url": "nope"})).status_code == 422
    assert len((await authed.get("/api/links")).json()) == 2
    assert (await authed.get("/api/documents")).json() == []  # links don't show up as documents


async def test_link_fetch_success_refresh_and_tags(authed, monkeypatch):
    from jobfinder.api import links

    texts = iter(["first summary", "second summary"])

    async def ok(url):
        return next(texts)

    monkeypatch.setattr(links, "fetch_github_summary", ok)
    r = await authed.post("/api/links", json={"kind": "github", "url": "https://github.com/me"})
    assert r.json()["fetched"] is True and r.json()["url"] == "https://github.com/me"
    link_id = r.json()["id"]
    assert (await authed.get(f"/api/documents/{link_id}/text")).text == "first summary"
    meta = (await authed.get(f"/api/documents/{link_id}")).json()
    assert meta["tags"]["link_kind"] == "github" and meta["tags"]["source"] == "link"

    assert (await authed.post(f"/api/links/{link_id}/refresh")).json()["fetched"] is True
    assert (await authed.get(f"/api/documents/{link_id}/text")).text == "second summary"
    assert (await authed.delete(f"/api/links/{link_id}")).status_code == 204
    assert (await authed.get("/api/links")).json() == []


async def test_documents_cannot_be_managed_through_link_endpoints(authed):
    doc = (await authed.post("/api/documents", files={"file": ("a.txt", b"hi", "text/plain")})).json()
    assert (await authed.post(f"/api/links/{doc['id']}/refresh")).status_code == 404
    assert (await authed.delete(f"/api/links/{doc['id']}")).status_code == 404
    assert (await authed.get(f"/api/documents/{doc['id']}")).status_code == 200
