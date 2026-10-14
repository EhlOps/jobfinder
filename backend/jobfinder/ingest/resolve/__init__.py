"""Turn a pasted job URL into the company's whole board.

Runs the resolver ladder (rungs/__init__.py; .plans/2026-10-13-adaptive-job-scraping): the cheapest
deterministic rungs first, the LLM last. The first rung that yields a board (known ATS) or a validated
recipe wins. A board found here is added with origin 'user' and shared with everyone; only the JobLink row
is private. If no board turns up but a rung found the posting itself, that one job is kept.
"""
import dataclasses
import hashlib
import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ingest.base import JobPosting
from jobfinder.ingest.resolve.ladder import BoardHit, RecipeHit, ResolveContext, SingleJob
from jobfinder.ingest.resolve.rungs import RUNGS
from jobfinder.ingest.service import ingest_company, upsert_postings
from jobfinder.ingest.validate import SLUG_RE, add_board
from jobfinder.models import Company, Job, JobLink

log = logging.getLogger("jobfinder.resolve")

MAX_URL_CHARS = 2048
SUPPORTED = "Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, BambooHR and Workday"
UNSUPPORTED = f"We can't read this site's job board yet. Links from {SUPPORTED} work today."
SINGLE_ONLY = "We saved this job, but couldn't find the rest of the company's job board yet."
_HEADERS = {"User-Agent": "JobFinder/0.1 (+personal job search)"}
LINK_SOURCE = "link"  # Job.source for a pasted posting kept on its own (no board)


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


def name_from_domain(domain: str) -> str:
    """'acme-robotics.com' -> 'Acme Robotics'."""
    return name_from_slug("", domain.split(".")[0]) if domain else "Unknown company"


def registrable_domain(url: str) -> str:
    """Best-effort registrable domain ('careers.acme.co.uk' -> 'acme.co.uk'). No public-suffix list, so
    common two-level suffixes are special-cased; good enough for naming and namespacing recipe boards."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "ac", "org", "net", "gov", "edu") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


async def existing_board(db: AsyncSession, ats: str, slug: str) -> Company | None:
    return await db.scalar(sa.select(Company).where(Company.ats == ats, Company.slug == slug))


async def resolve_link(db: AsyncSession, link_id: int, client: httpx.AsyncClient | None = None, *,
                       allow_llm: bool = False) -> dict:
    """Resolve one pasted link: find its board, add it if it's new, then fetch that board right away.
    Never raises for a bad board: the outcome is recorded on the link so the user sees why."""
    link = await db.get(JobLink, link_id)
    if link is None:
        return {"skipped": "link deleted"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS)
    try:
        return await _resolve(db, link, client, allow_llm)
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


def _finish(link: JobLink, status: str, *, company: Company | None = None, error: str | None = None,
            rung: str | None = None) -> None:
    link.status = status
    link.error = error[:500] if error else None
    if company is not None:
        link.company_id = company.id
    if rung is not None:
        link.rung = rung
    link.resolved_at = datetime.now(UTC)


async def _board_company(db: AsyncSession, hit: BoardHit, client: httpx.AsyncClient) -> tuple[Company | None, bool, str]:
    """(company, newly_added, reason). Reuses an existing board; otherwise validates and adds it."""
    company = await existing_board(db, hit.ats, hit.slug)
    if company is not None:
        return company, False, ""
    name = hit.name or name_from_slug(hit.ats, hit.slug)
    company, reason = await add_board(db, hit.ats, hit.slug, name, origin="user", client=client)
    if company is None and reason.startswith("duplicate"):  # another paste of the same board won the race
        company = await existing_board(db, hit.ats, hit.slug)
        return company, False, reason
    return company, company is not None, reason


async def _recipe_company(db: AsyncSession, hit: RecipeHit) -> tuple[Company | None, bool, str]:
    """Store a validated recipe as an ats='recipe' board keyed by domain. An existing recipe board for the
    domain is kept (its recipe heals through its own lifecycle, not by a user's paste)."""
    slug = hit.domain.lower()
    if not SLUG_RE.match(slug):
        return None, False, f"invalid domain '{slug}'"
    company = await existing_board(db, "recipe", slug)
    if company is not None:
        return company, False, ""
    company = Company(
        name=(hit.name or name_from_domain(slug))[:200], ats="recipe", slug=slug, origin="user",
        validated_at=datetime.now(UTC), recipe=hit.recipe.model_dump(mode="json"), recipe_state="validated",
        recipe_health={"baseline_yield": len(hit.postings), "last_yield": len(hit.postings)},
    )
    db.add(company)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return await existing_board(db, "recipe", slug), False, "duplicate"
    return company, True, ""


async def _keep_single_job(db: AsyncSession, link: JobLink, posting: JobPosting) -> int | None:
    """Store the pasted posting on its own (source 'link', no company) and point the link at it."""
    eid = hashlib.sha1(link.url.encode()).hexdigest()
    p = dataclasses.replace(posting, source=LINK_SOURCE, external_id=eid, url=posting.url or link.url)
    await upsert_postings(db, [p], None)
    job_id = await db.scalar(sa.select(Job.id).where(Job.source == LINK_SOURCE, Job.external_id == eid))
    link.job_id = job_id
    return job_id


async def _resolve(db: AsyncSession, link: JobLink, client: httpx.AsyncClient, allow_llm: bool) -> dict:
    ctx = ResolveContext(link.url, client, allow_llm=allow_llm)
    board_error = None
    for rung in RUNGS:
        try:
            hit = await rung.run(ctx)
        except Exception as e:  # a broken rung passes; the next one gets a go
            log.warning("rung %s failed on link %s: %s", rung.name, link.id, e)
            ctx.note(f"{rung.name}: {type(e).__name__}")
            continue
        if hit is None:
            continue
        if isinstance(hit, SingleJob):
            ctx.single_job = ctx.single_job or hit.posting
            continue
        if isinstance(hit, BoardHit):
            link.ats, link.slug = hit.ats, hit.slug
            company, added, reason = await _board_company(db, hit, client)
        else:
            link.ats, link.slug = "recipe", hit.domain
            company, added, reason = await _recipe_company(db, hit)
        if company is None:
            board_error = f"That board couldn't be read: {reason}"
            ctx.note(f"{rung.name}: {reason}")
            continue
        return await _done(db, link, company, added, rung.name, client)

    if ctx.single_job is not None:
        job_id = await _keep_single_job(db, link, ctx.single_job)
        _finish(link, "unresolved", error=SINGLE_ONLY, rung="single_job")
        await db.commit()
        return {"status": "unresolved", "reason": "single job only", "job_id": job_id, "notes": ctx.notes}
    _finish(link, "unresolved", error=board_error or UNSUPPORTED)
    await db.commit()
    return {"status": "unresolved", "reason": "unsupported" if not board_error else board_error,
            "notes": ctx.notes}


async def _done(db: AsyncSession, link: JobLink, company: Company, added: bool, rung: str,
                client: httpx.AsyncClient) -> dict:
    stats = None
    if added:  # a known board is already being fetched on schedule
        stats = await ingest_company(db, client, company)
        company = await db.get(Company, company.id)
    note = None if company.enabled else "This company's board is switched off right now, so its jobs may be out of date."
    _finish(link, "resolved", company=company, error=note, rung=rung)
    await db.commit()
    out = {"status": "resolved", "company_id": company.id, "added": added, "rung": rung}
    if stats is not None:
        out.update(fetched=stats.fetched, new=stats.new, error=stats.error)
    return out
