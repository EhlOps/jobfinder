"""Profile links (GitHub, portfolio, LinkedIn, ...): fetch what we may, and keep each link and its text
in the document store as a "link" document.

Adding or refreshing a link saves it as "processing" and queues worker-io's fetch_link task (`fetch`), the
only database write (#51); LinkedIn and X links are only saved. The fetchers are imported where they run,
so the api process doesn't load them.
"""
from urllib.parse import urlparse

from jobfinder.core.db import AsyncSession
from jobfinder.core.errors import NotFound
from jobfinder.schemas.links import LinkUpload, ProfileLinkOut
from jobfinder.storage import documents as storage
from jobfinder.storage.documents import DocumentMeta, DocumentStore
from jobfinder.tasks import ledger

# LinkedIn and X block scraping; we store the URL but only fetch github/portfolio/other.
NO_FETCH = {"linkedin", "x"}


async def _fetch(kind: str, url: str) -> tuple[str, str | None]:
    """Returns (text, error). Failures are recorded on the link, never raised."""
    from jobfinder.integrations.github import GitHubError, fetch_github_summary
    from jobfinder.profile.web_page import FetchError, fetch_page_text

    if kind in NO_FETCH:
        return "", None
    try:
        text = await (fetch_github_summary(url) if kind == "github" else fetch_page_text(url))
        return text, None
    except (GitHubError, FetchError) as e:
        return "", str(e)[:500]
    except Exception:
        return "", "Could not fetch this URL"


def _status(error: str | None) -> str:
    return "error" if error else "ready"


def _link(store: DocumentStore, user_id: int, link_id: str) -> DocumentMeta:
    try:
        meta = store.get(user_id, link_id)
    except storage.NotFound:
        raise NotFound("Not found") from None
    if meta.kind != "link":
        raise NotFound("Not found")
    return meta


async def _queue(db: AsyncSession, user_id: int, link_id: str) -> int:
    # rerun: a refresh while a fetch runs queues one more after it, since that one may have fetched already
    return (await ledger.add(db, "fetch_link", {"doc_id": link_id}, user_id=user_id, rerun=True)).id


def list_links(store: DocumentStore, user_id: int) -> list[DocumentMeta]:
    return store.list(user_id, kind="link")


async def add(db: AsyncSession, store: DocumentStore, user_id: int, kind: str, url: str) -> LinkUpload:
    tags = {"source": "link", "url": url, "link_kind": kind}
    name = urlparse(url).netloc or url
    if kind in NO_FETCH:  # only saved: nothing to wait for
        meta, _ = store.put(user_id, "link", name, text="", content_type="text/plain", tags=tags, status="ready")
        return LinkUpload(link=ProfileLinkOut.of(meta), task_id=None)
    meta, _ = store.put(user_id, "link", name, text="", content_type="text/plain", tags=tags, status="processing")
    return LinkUpload(link=ProfileLinkOut.of(meta), task_id=await _queue(db, user_id, meta.id))


async def refresh(db: AsyncSession, store: DocumentStore, user_id: int, link_id: str) -> LinkUpload:
    """Fetch the link again. Its old text stays until the new fetch lands."""
    meta = _link(store, user_id, link_id)
    kind = meta.tags.get("link_kind", "other")
    if kind in NO_FETCH:  # never fetched: nothing to wait for
        meta = store.replace_text(user_id, link_id, "", fetch_error=None, fetched=False, status="ready")
        return LinkUpload(link=ProfileLinkOut.of(meta), task_id=None)
    meta = store.set_status(user_id, link_id, "processing")
    return LinkUpload(link=ProfileLinkOut.of(meta), task_id=await _queue(db, user_id, link_id))


async def fetch(store: DocumentStore, user_id: int, link_id: str) -> dict:
    """worker-io's fetch_link: fetch the link (SSRF-guarded) and record its text, or the error. A link
    deleted meanwhile is left alone."""
    try:
        meta = _link(store, user_id, link_id)
    except NotFound:
        return {"doc_id": link_id, "skipped": "deleted"}
    text, error = await _fetch(meta.tags.get("link_kind", "other"), meta.tags.get("url", ""))
    try:
        meta = store.replace_text(user_id, link_id, text, fetch_error=error, fetched=bool(text), status=_status(error))
    except (storage.NotFound, FileNotFoundError):  # deleted while we fetched it
        return {"doc_id": link_id, "skipped": "deleted"}
    return {"doc_id": link_id, "status": meta.status, "chars": meta.chars, "error": error}


def delete(store: DocumentStore, user_id: int, link_id: str) -> None:
    _link(store, user_id, link_id)
    try:
        store.delete(user_id, link_id)
    except storage.NotFound:
        raise NotFound("Not found") from None
