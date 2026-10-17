"""Turn a pasted job URL into the company's whole board.

Runs the resolver ladder (rungs/__init__.py; .plans/2026-10-13-adaptive-job-scraping): the cheapest
deterministic rungs first, the LLM last. The first rung that yields a board (known ATS) or a validated
recipe wins. A board found here is added with origin 'user' and shared with everyone; only the JobLink row
is private. If no board turns up but a rung found the posting itself, that one job is kept.

Nothing here commits: the resolve_job_link task (or the coverage audit) is the boundary.
"""
import dataclasses
import hashlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.http import USER_AGENT
from jobfinder.core.urls import (  # noqa: F401  (re-exported: the API and tests import them from here)
    MAX_URL_CHARS,
    BadLink,
    clean_url,
    registrable_domain,
)
from jobfinder.ingest.base import JobPosting
from jobfinder.ingest.http_safety import SafeResponse
from jobfinder.ingest.validate import SLUG_RE, add_board
from jobfinder.integrations.ats.custom import refusal_for_url, refusal_message, site_for_url
from jobfinder.integrations.robots import PoliteFetcher
from jobfinder.models import Company, JobLink
from jobfinder.repositories import boards as boards_repo
from jobfinder.repositories import postings as postings_repo
from jobfinder.services.ingestion import ingest_company, upsert_postings
from jobfinder.services.resolver.ladder import BoardHit, RecipeHit, ResolveContext, SingleJob
from jobfinder.services.resolver.names import name_from_domain, name_from_slug
from jobfinder.services.resolver.rungs import RUNGS

if TYPE_CHECKING:
    from jobfinder.integrations.claude.tasks import AITasks

log = logging.getLogger("jobfinder.resolve")

SUPPORTED = (
    "Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, BambooHR, Workday, iCIMS, "
    "Oracle Recruiting, SuccessFactors, Eightfold, Phenom and Taleo"
)
UNSUPPORTED = (
    "We can't read this site's job board yet. Links from career sites on "
    f"{SUPPORTED} work best; we also try the company's own careers pages."
)
SINGLE_ONLY = "We saved this job, but couldn't find the rest of the company's job board yet."
_HEADERS = {"User-Agent": USER_AGENT}
LINK_SOURCE = "link"  # Job.source for a pasted posting kept on its own (no board)


async def existing_board(db: AsyncSession, ats: str, slug: str) -> Company | None:
    return await boards_repo.existing(db, ats, slug)


async def resolve_link(db: AsyncSession, link_id: int, client: httpx.AsyncClient | None = None, *,
                       allow_llm: bool = False, ai: "AITasks | None" = None) -> dict:
    """Resolve one pasted link: find its board, add it if it's new, then fetch that board right away.
    Never raises for a bad board: the outcome is recorded on the link so the user sees why."""
    link = await db.get(JobLink, link_id)
    if link is None:
        return {"skipped": "link deleted"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS)
    try:
        async with db.begin_nested():  # a failure undoes this resolve's writes, not the caller's
            return await _resolve(db, link, client, allow_llm, ai)
    except Exception as e:  # never leave a link stuck in 'resolving'
        log.exception("resolving job link %s failed", link_id)
        link = await db.get(JobLink, link_id)
        if link is not None:
            _finish(link, "unresolved", error=f"Something went wrong while reading that board ({type(e).__name__}).")
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
    try:
        async with db.begin_nested():  # a lost race rolls back only this insert
            db.add(company)
    except IntegrityError:
        return await existing_board(db, "recipe", slug), False, "duplicate"
    return company, True, ""


async def _keep_single_job(db: AsyncSession, link: JobLink, posting: JobPosting) -> int | None:
    """Store the pasted posting on its own (source 'link', no company) and point the link at it."""
    eid = hashlib.sha1(link.url.encode()).hexdigest()
    p = dataclasses.replace(posting, source=LINK_SOURCE, external_id=eid, url=posting.url or link.url)
    await upsert_postings(db, [p], None)
    job_id = await postings_repo.id_by_external(db, LINK_SOURCE, eid)
    link.job_id = job_id
    return job_id


@dataclasses.dataclass
class _Outcome:
    """What one pass of the ladder over a URL found."""
    ctx: ResolveContext
    company: Company | None = None
    added: bool = False
    rung: str | None = None
    hit: tuple[str, str] | None = None  # (ats, slug) of the last board or recipe hit tried
    board_error: str | None = None


async def _ladder(db: AsyncSession, url: str, client: httpx.AsyncClient, allow_llm: bool, *,
                  page: SafeResponse | None = None, robots: PoliteFetcher | None = None, label: str = "",
                  ai: "AITasks | None" = None) -> _Outcome:
    """Run the rungs over `url`, cheapest first, until one yields a board that can be added or reused.
    `page` (already fetched, SSRF- and robots-checked) saves the context fetching it again."""
    ctx = ResolveContext(url, client, allow_llm=allow_llm, robots=robots, ai=ai)
    if page is not None:  # what ctx.page() would have cached after a successful fetch
        ctx._page, ctx._fetched = page, True
    out = _Outcome(ctx)
    for rung in RUNGS:
        if rung.name == "llm" and ctx.allow_llm:
            # a recipe board for this domain already exists and would be kept (_recipe_company): use it
            # rather than paying for an induction whose result is thrown away
            known = await existing_board(db, "recipe", registrable_domain(url))
            if known is not None:
                out.hit, out.company, out.added, out.rung = ("recipe", known.slug), known, False, "recipe_board"
                return out
        try:
            hit = await rung.run(ctx)
        except Exception as e:  # a broken rung passes; the next one gets a go
            log.warning("rung %s failed on %s: %s", rung.name, label or url, e)
            ctx.note(f"{rung.name}: {type(e).__name__}")
            continue
        if hit is None:
            continue
        if isinstance(hit, SingleJob):
            ctx.single_job = ctx.single_job or hit.posting
            continue
        if isinstance(hit, BoardHit):
            out.hit = (hit.ats, hit.slug)
            company, added, reason = await _board_company(db, hit, client)
        else:
            out.hit = ("recipe", hit.domain)
            company, added, reason = await _recipe_company(db, hit)
        if company is None:
            out.board_error = f"That board couldn't be read: {reason}"
            ctx.note(f"{rung.name}: {reason}")
            if isinstance(hit, BoardHit) and site_for_url(url) is not None:
                break  # a reviewed custom site maps to its one board; its own pages are not to be scraped
            continue
        out.company, out.added, out.rung = company, added, rung.name
        return out
    return out


async def resolve_url(db: AsyncSession, url: str, client: httpx.AsyncClient, *, allow_llm: bool = False,
                      page: SafeResponse | None = None, robots: PoliteFetcher | None = None,
                      ai: "AITasks | None" = None) -> tuple[Company | None, str | None, list[str]]:
    """Resolve a URL with no JobLink behind it (the coverage audit): (company, rung, notes). A board that
    is new is added with origin 'user' and fetched right away, as for a pasted link."""
    refused = refusal_for_url(url)
    if refused is not None:  # terms/robots review said no: nothing here may fetch or scrape this site
        return None, None, [refusal_message(refused)]
    out = await _ladder(db, url, client, allow_llm, page=page, robots=robots, ai=ai)
    company = out.company
    if company is not None and out.added:
        await ingest_company(db, client, company)
        company = await db.get(Company, company.id)
    notes = list(out.ctx.notes)
    if company is None and out.board_error and out.board_error not in notes:
        notes.append(out.board_error)
    return company, out.rung, notes


async def _resolve(db: AsyncSession, link: JobLink, client: httpx.AsyncClient, allow_llm: bool,
                   ai: "AITasks | None" = None) -> dict:
    refused = refusal_for_url(link.url)
    if refused is not None:  # terms/robots review said no: no rung may fetch or scrape this site
        _finish(link, "unresolved", error=refusal_message(refused), rung="terms_refused")
        return {"status": "unresolved", "reason": "terms refused", "site": refused.name}
    out = await _ladder(db, link.url, client, allow_llm, label=f"link {link.id}", ai=ai)
    ctx = out.ctx
    if out.hit is not None:
        link.ats, link.slug = out.hit
    if out.company is not None:
        return await _done(db, link, out.company, out.added, out.rung, client)

    if ctx.single_job is not None:
        job_id = await _keep_single_job(db, link, ctx.single_job)
        _finish(link, "unresolved", error=SINGLE_ONLY, rung="single_job")
        return {"status": "unresolved", "reason": "single job only", "job_id": job_id, "notes": ctx.notes}
    board_error = out.board_error
    _finish(link, "unresolved", error=board_error or UNSUPPORTED)
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
    out = {"status": "resolved", "company_id": company.id, "added": added, "rung": rung}
    if stats is not None:
        out.update(fetched=stats.fetched, new=stats.new, error=stats.error)
    return out
