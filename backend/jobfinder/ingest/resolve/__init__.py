"""Turn a pasted job URL into the company's whole board.

Rung 1 of the resolver ladder (.plans/2026-10-13-adaptive-job-scraping): the URL itself names a known ATS
board. Later rungs (embedded ATS, JSON-LD, listing sniff, Playwright, LLM recipe) plug in after it.

A board found here is added with origin 'user' and shared with everyone; only the JobLink row is private.
"""
import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ingest.discovery import board_from_url
from jobfinder.ingest.service import ingest_company
from jobfinder.ingest.validate import add_board
from jobfinder.models import Company, JobLink

log = logging.getLogger("jobfinder.resolve")

MAX_URL_CHARS = 2048
SUPPORTED = "Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, BambooHR and Workday"
UNSUPPORTED = f"We can't read this site's job board yet. Links from {SUPPORTED} work today."
_HEADERS = {"User-Agent": "JobFinder/0.1 (+personal job search)"}


class BadLink(ValueError):
    """The pasted text is not a URL we accept. The message is safe to show the user."""


def clean_url(raw: str) -> str:
    """Normalise a pasted job URL, or raise BadLink. Never fetched here; this is input hygiene only:
    http(s) only, no credentials, no control characters, bounded length, fragment dropped."""
    url = (raw or "").strip()
    if not url or len(url) > MAX_URL_CHARS:
        raise BadLink(f"Paste a job link of at most {MAX_URL_CHARS} characters.")
    if re.search(r"[\x00-\x20\x7f]", url):
        raise BadLink("That link contains spaces or control characters.")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as e:
        raise BadLink("That doesn't look like a link.") from e
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise BadLink("Only http(s) job links are supported.")
    if parts.username is not None or parts.password is not None:
        raise BadLink("Links with a username or password in them are not accepted.")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def name_from_slug(ats: str, slug: str) -> str:
    """A readable company name from the board slug ('acme-robotics' -> 'Acme Robotics'). Admins can rename."""
    base = slug.split(".")[0] if ats == "workday" else slug
    words = [w for w in re.split(r"[-_.\s]+", base) if w]
    return (" ".join(w if w.isupper() else w.capitalize() for w in words) or slug)[:200]


async def existing_board(db: AsyncSession, ats: str, slug: str) -> Company | None:
    return await db.scalar(sa.select(Company).where(Company.ats == ats, Company.slug == slug))


async def resolve_link(db: AsyncSession, link_id: int, client: httpx.AsyncClient | None = None) -> dict:
    """Resolve one pasted link: add its board if it's new, then fetch that board right away.
    Never raises for a bad board: the outcome is recorded on the link so the user sees why."""
    link = await db.get(JobLink, link_id)
    if link is None:
        return {"skipped": "link deleted"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS)
    try:
        return await _resolve(db, link, client)
    except Exception as e:  # never leave a link stuck in 'resolving'
        log.exception("resolving job link %s failed", link_id)
        await db.rollback()
        link = await db.get(JobLink, link_id)
        if link is not None:
            _finish(link, "unresolved", error=f"Something went wrong while reading that board ({type(e).__name__}).")
            await db.commit()
        return {"error": type(e).__name__}
    finally:
        if own:
            await client.aclose()


def _finish(link: JobLink, status: str, *, company: Company | None = None, error: str | None = None) -> None:
    link.status = status
    link.error = error[:500] if error else None
    if company is not None:
        link.company_id = company.id
    link.resolved_at = datetime.now(UTC)


async def _resolve(db: AsyncSession, link: JobLink, client: httpx.AsyncClient) -> dict:
    board = board_from_url(link.url)
    if board is None:
        _finish(link, "unresolved", error=UNSUPPORTED)
        await db.commit()
        return {"status": "unresolved", "reason": "unsupported"}
    ats, slug = board
    link.ats, link.slug = ats, slug

    company = await existing_board(db, ats, slug)
    added = False
    if company is None:
        company, reason = await add_board(db, ats, slug, name_from_slug(ats, slug), origin="user", client=client)
        if company is None and reason.startswith("duplicate"):  # another paste of the same board won the race
            company = await existing_board(db, ats, slug)
        if company is None:
            _finish(link, "unresolved", error=f"That board couldn't be read: {reason}")
            await db.commit()
            return {"status": "unresolved", "reason": reason}
        added = True

    stats = None
    if added:  # a known board is already being fetched on schedule
        stats = await ingest_company(db, client, company)
        company = await db.get(Company, company.id)
    note = None if company.enabled else "This company's board is switched off right now, so its jobs may be out of date."
    _finish(link, "resolved", company=company, error=note)
    await db.commit()
    out = {"status": "resolved", "company_id": company.id, "added": added}
    if stats is not None:
        out.update(fetched=stats.fetched, new=stats.new, error=stats.error)
    return out
