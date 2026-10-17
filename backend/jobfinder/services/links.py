"""Profile links (GitHub, portfolio, LinkedIn, ...): fetch what we may, and keep each link and its text
in the document store as a "link" document. Store-only, no database."""
from urllib.parse import urlparse

from jobfinder.core.errors import NotFound
from jobfinder.integrations.github import GitHubError, fetch_github_summary
from jobfinder.profile.web_page import FetchError, fetch_page_text
from jobfinder.storage import documents as storage
from jobfinder.storage.documents import DocumentMeta, DocumentStore

# LinkedIn and X block scraping; we store the URL but only fetch github/portfolio/other.
NO_FETCH = {"linkedin", "x"}


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


def _link(store: DocumentStore, user_id: int, link_id: str) -> DocumentMeta:
    try:
        meta = store.get(user_id, link_id)
    except storage.NotFound:
        raise NotFound("Not found") from None
    if meta.kind != "link":
        raise NotFound("Not found")
    return meta


def list_links(store: DocumentStore, user_id: int) -> list[DocumentMeta]:
    return store.list(user_id, kind="link")


async def add(store: DocumentStore, user_id: int, kind: str, url: str) -> DocumentMeta:
    text, error = await _fetch(kind, url)
    meta, _ = store.put(
        user_id, "link", urlparse(url).netloc or url,
        text=text, content_type="text/plain", fetch_error=error, fetched=bool(text),
        tags={"source": "link", "url": url, "link_kind": kind},
    )
    return meta


async def refresh(store: DocumentStore, user_id: int, link_id: str) -> DocumentMeta:
    meta = _link(store, user_id, link_id)
    text, error = await _fetch(meta.tags.get("link_kind", "other"), meta.tags.get("url", ""))
    return store.replace_text(user_id, link_id, text, fetch_error=error, fetched=bool(text))


def delete(store: DocumentStore, user_id: int, link_id: str) -> None:
    _link(store, user_id, link_id)
    try:
        store.delete(user_id, link_id)
    except storage.NotFound:
        raise NotFound("Not found") from None
