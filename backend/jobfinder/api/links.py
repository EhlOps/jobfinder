from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException

from jobfinder.api.deps import CurrentUser
from jobfinder.profile.github import GitHubError, fetch_github_summary
from jobfinder.profile.web_page import FetchError, fetch_page_text
from jobfinder.schemas.links import ProfileLinkIn, ProfileLinkOut
from jobfinder.storage.documents import DocumentStore, NotFound, get_store

router = APIRouter(prefix="/api/links", tags=["links"])

# LinkedIn and X block scraping; we store the URL but only fetch github/portfolio/other.
NO_FETCH = {"linkedin", "x"}

Store = Annotated[DocumentStore, Depends(get_store)]


async def _fetch(kind: str, url: str) -> tuple[str, str | None]:
    """Returns (text, error). Failures are recorded on the link, never raised."""
    if kind in NO_FETCH:
        return "", None
    try:
        text = await (fetch_github_summary(url) if kind == "github" else fetch_page_text(url))
        return text, None
    except (GitHubError, FetchError) as e:
        return "", str(e)[:500]
    except Exception:
        return "", "Could not fetch this URL"


@router.get("", response_model=list[ProfileLinkOut])
async def list_links(user: CurrentUser, store: Store):
    return [ProfileLinkOut.of(m) for m in store.list(user.id, kind="link")]


@router.post("", response_model=ProfileLinkOut, status_code=201)
async def add_link(body: ProfileLinkIn, user: CurrentUser, store: Store):
    url = str(body.url)
    text, error = await _fetch(body.kind, url)
    meta, _ = store.put(
        user.id, "link", urlparse(url).netloc or url,
        text=text, content_type="text/plain", fetch_error=error, fetched=bool(text),
        tags={"source": "link", "url": url, "link_kind": body.kind},
    )
    return ProfileLinkOut.of(meta)


@router.post("/{link_id}/refresh", response_model=ProfileLinkOut)
async def refresh_link(link_id: str, user: CurrentUser, store: Store):
    try:
        meta = store.get(user.id, link_id)
    except NotFound:
        raise HTTPException(404, "Not found") from None
    if meta.kind != "link":
        raise HTTPException(404, "Not found")
    text, error = await _fetch(meta.tags.get("link_kind", "other"), meta.tags.get("url", ""))
    return ProfileLinkOut.of(store.replace_text(user.id, link_id, text, fetch_error=error, fetched=bool(text)))


@router.delete("/{link_id}", status_code=204)
async def delete_link(link_id: str, user: CurrentUser, store: Store):
    try:
        if store.get(user.id, link_id).kind != "link":
            raise NotFound("not a link")
        store.delete(user.id, link_id)
    except NotFound:
        raise HTTPException(404, "Not found") from None
