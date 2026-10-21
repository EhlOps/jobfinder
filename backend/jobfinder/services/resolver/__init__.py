"""Turn a pasted job URL into the company's whole board.

Runs the resolver ladder (rungs/__init__.py; .plans/2026-10-13-adaptive-job-scraping): the cheapest
deterministic rungs first, the LLM last. The first rung that yields a board (known ATS) or a validated
recipe wins. A board found here is added with origin 'user' and shared with everyone; only the JobLink row
is private. If no board turns up but a rung found the posting itself, that one job is kept.

The ladder runs in two tasks (#50). resolve_link runs the deterministic rungs (resolve_job_link, on io).
When none reads the site and the LLM is allowed, the link stays 'resolving' and the result carries what
they found; induce_link then runs the LLM rung with it (induce_recipe, on ai) and finishes the link.

Nothing here commits: those tasks (or the coverage audit) are the boundaries.
"""
import dataclasses
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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
from jobfinder.integrations.ats import SLUG_RE
from jobfinder.integrations.http_safety import SafeResponse
from jobfinder.integrations.robots import PoliteFetcher
from jobfinder.integrations.site_terms import refusal_for_url, refusal_message, site_for_url
from jobfinder.models import Company, JobLink
from jobfinder.models.posting import JobPosting
from jobfinder.repositories import boards as boards_repo
from jobfinder.repositories import postings as postings_repo
from jobfinder.services.board_validation import add_board
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
MAX_TRIED_HINTS = 50  # URLs the deterministic rungs tried as JSON, carried over to induce_link


async def existing_board(db: AsyncSession, ats: str, slug: str) -> Company | None:
    return await boards_repo.existing(db, ats, slug)


async def llm_allowed(db: AsyncSession) -> bool:
    """The resolver may ask Claude unless it's switched off or the planner is backing off a rate limit."""
    from jobfinder.core.config import settings
    from jobfinder.scheduling import planner

    return settings.resolver_llm_enabled and await planner.backoff_until(db, datetime.now(UTC)) is None


async def resolve_link(db: AsyncSession, link_id: int, client: httpx.AsyncClient | None = None, *,
                       allow_llm: bool = False) -> dict:
    """Resolve one pasted link with the deterministic rungs: find its board, add it if it's new, then fetch
    that board right away. Never raises for a bad board: the outcome is recorded on the link so the user
    sees why. With `allow_llm`, a link no rung could read stays 'resolving' and the result's "induce" holds
    the arguments for induce_link (#50)."""
    return await _guarded(db, link_id, client, lambda link, c: _resolve(db, link, c, allow_llm))


async def induce_link(db: AsyncSession, link_id: int, ai: "AITasks | None", client: httpx.AsyncClient | None = None,
                      *, allow_llm: bool = True, retry: bool = False, samples: list = (), tried: list[str] = (),
                      notes: list[str] = (), single_job: dict | None = None, board_error: str | None = None) -> dict:
    """The LLM rung for a link resolve_link left 'resolving' (#50), given what the deterministic rungs found:
    their JSON samples and the URLs they tried, their notes, a single posting, a board that couldn't be read.
    Finishes the link as resolve_link would have, except that with `retry` a retryable AI error leaves it
    'resolving' and returns "retry": True for the caller to try again later."""
    hints = {"samples": samples, "tried": tried, "notes": notes, "single_job": single_job,
             "board_error": board_error}
    return await _guarded(db, link_id, client, lambda link, c: _induce(db, link, c, ai, allow_llm, retry, **hints))


async def _guarded(db: AsyncSession, link_id: int, client: httpx.AsyncClient | None,
                   run: Callable[[JobLink, httpx.AsyncClient], Awaitable[dict]]) -> dict:
    link = await db.get(JobLink, link_id)
    if link is None:
        return {"skipped": "link deleted"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS)
    try:
        async with db.begin_nested():  # a failure undoes this resolve's writes, not the caller's
            return await run(link, client)
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
    stopped: bool = False  # a reviewed custom site's board failed: no later rung may scrape its pages


async def _take_hit(db: AsyncSession, out: _Outcome, rung: str, hit: BoardHit | RecipeHit,
                    client: httpx.AsyncClient) -> bool:
    """Add or reuse the board behind a hit. False, with the reason noted, when it can't be."""
    if isinstance(hit, BoardHit):
        out.hit = (hit.ats, hit.slug)
        company, added, reason = await _board_company(db, hit, client)
    else:
        out.hit = ("recipe", hit.domain)
        company, added, reason = await _recipe_company(db, hit)
    if company is None:
        out.board_error = f"That board couldn't be read: {reason}"
        out.ctx.note(f"{rung}: {reason}")
        return False
    out.company, out.added, out.rung = company, added, rung
    return True


async def _ladder(db: AsyncSession, url: str, client: httpx.AsyncClient, *, page: SafeResponse | None = None,
                  robots: PoliteFetcher | None = None, label: str = "") -> _Outcome:
    """Run the deterministic rungs over `url`, cheapest first, until one yields a board that can be added or
    reused; the LLM rung is induce_link's (#50). `page` (already fetched, SSRF- and robots-checked) saves
    the context fetching it again."""
    ctx = ResolveContext(url, client, robots=robots)
    if page is not None:  # what ctx.page() would have cached after a successful fetch
        ctx._page, ctx._fetched = page, True
    out = _Outcome(ctx)
    for rung in RUNGS:
        if rung.name == "llm":
            continue
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
        if await _take_hit(db, out, rung.name, hit, client):
            return out
        if isinstance(hit, BoardHit) and site_for_url(url) is not None:
            out.stopped = True
            break  # a reviewed custom site maps to its one board; its own pages are not to be scraped
    return out


async def resolve_url(db: AsyncSession, url: str, client: httpx.AsyncClient, *, page: SafeResponse | None = None,
                      robots: PoliteFetcher | None = None) -> tuple[Company | None, str | None, list[str]]:
    """Resolve a URL with no JobLink behind it (the coverage audit): (company, rung, notes). A board that
    is new is added with origin 'user' and fetched right away, as for a pasted link. Deterministic rungs
    only: the audit runs on io and never asks Claude (#50)."""
    refused = refusal_for_url(url)
    if refused is not None:  # terms/robots review said no: nothing here may fetch or scrape this site
        return None, None, [refusal_message(refused)]
    out = await _ladder(db, url, client, page=page, robots=robots)
    company = out.company
    if company is not None and out.added:
        await ingest_company(db, client, company)
        company = await db.get(Company, company.id)
    notes = list(out.ctx.notes)
    if company is None and out.board_error and out.board_error not in notes:
        notes.append(out.board_error)
    return company, out.rung, notes


async def _resolve(db: AsyncSession, link: JobLink, client: httpx.AsyncClient, allow_llm: bool) -> dict:
    refused = refusal_for_url(link.url)
    if refused is not None:  # terms/robots review said no: no rung may fetch or scrape this site
        _finish(link, "unresolved", error=refusal_message(refused), rung="terms_refused")
        return {"status": "unresolved", "reason": "terms refused", "site": refused.name}
    out = await _ladder(db, link.url, client, label=f"link {link.id}")
    if out.hit is not None:
        link.ats, link.slug = out.hit
    if out.company is not None:
        return await _done(db, link, out.company, out.added, out.rung, client)
    if allow_llm and not out.stopped:
        if (known := await _known_recipe_board(db, link)) is not None:
            return await _done(db, link, known, False, "recipe_board", client)
        if out.ctx.page_error is None:  # an unreadable page: the LLM rung would get none either
            # the link stays 'resolving' (the SPA keeps polling) until induce_recipe finishes it
            return {"status": "resolving", "notes": out.ctx.notes, "induce": _hints(out)}
    return await _unresolved(db, link, out.ctx, out.board_error)


async def _known_recipe_board(db: AsyncSession, link: JobLink) -> Company | None:
    """A recipe board that already exists for the link's domain would be kept (_recipe_company): use it
    rather than paying for an induction whose result is thrown away."""
    known = await existing_board(db, "recipe", registrable_domain(link.url))
    if known is not None:
        link.ats, link.slug = "recipe", known.slug
    return known


def _hints(out: _Outcome) -> dict[str, Any]:
    """induce_link's arguments from the deterministic pass, JSON-safe for the induce_recipe payload. The
    samples are carried, not fetched again: they are small (MAX_SAMPLE_CHARS) and some came through the renderer."""
    from jobfinder.services.resolver.rungs.llm import MAX_SAMPLE_FETCHES

    ctx, job = out.ctx, None
    if ctx.single_job is not None:
        job = dataclasses.asdict(ctx.single_job)
        job["posted_at"] = job["posted_at"].isoformat() if job["posted_at"] else None
    samples = ctx.json_samples[:MAX_SAMPLE_FETCHES]
    tried = [u for u, _ in samples] + sorted(ctx.json_tried - {u for u, _ in samples})  # the samples' own first
    return _no_nul({"samples": [list(s) for s in samples], "tried": tried[:MAX_TRIED_HINTS],
                    "notes": list(ctx.notes), "single_job": job, "board_error": out.board_error})


def _no_nul(v):
    """Scraped text without NUL characters, which Postgres can't store in jsonb."""
    if isinstance(v, str):
        return v.replace("\x00", "")
    if isinstance(v, dict):
        return {k: _no_nul(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_no_nul(x) for x in v]
    return v


def _posting(job: dict) -> JobPosting:
    names = {f.name for f in dataclasses.fields(JobPosting)}
    job = {k: v for k, v in job.items() if k in names}
    if job.get("posted_at"):
        job["posted_at"] = datetime.fromisoformat(job["posted_at"])
    return JobPosting(**job)


async def _induce(db: AsyncSession, link: JobLink, client: httpx.AsyncClient, ai: "AITasks | None",
                  allow_llm: bool, retry: bool, *, samples, tried, notes, single_job, board_error) -> dict:
    from jobfinder.services.resolver.rungs.llm import LlmRung

    if link.status != "resolving":  # finished already (a duplicate delivery, or a re-paste resolved it)
        return {"skipped": f"link already {link.status}"}
    if (known := await _known_recipe_board(db, link)) is not None:  # one may have appeared since
        return await _done(db, link, known, False, "recipe_board", client)
    ctx = ResolveContext(link.url, client, allow_llm=allow_llm, ai=ai)
    ctx.notes = list(notes)
    ctx.json_samples = [(u, body) for u, body in samples]
    ctx.json_tried = set(tried)
    ctx.single_job = _posting(single_job) if single_job else None
    out = _Outcome(ctx, board_error=board_error)
    hit = None
    if not allow_llm:
        ctx.note("llm: not allowed now (switched off or backing off a rate limit)")
    else:
        try:
            hit = await LlmRung().run(ctx)
        except Exception as e:  # as in _ladder: a broken rung passes
            log.warning("rung llm failed on link %s: %s", link.id, e)
            ctx.note(f"llm: {type(e).__name__}")
    if hit is None and retry and ctx.ai_error is not None and ctx.ai_error.retryable:
        # not the site's fault (a rate limit, a timeout): the link waits for another go
        return {"retry": True, "reason": f"AI unavailable ({ctx.ai_error.kind})", "notes": ctx.notes}
    if hit is not None:
        took = await _take_hit(db, out, "llm", hit, client)
        link.ats, link.slug = out.hit
        if took:
            return await _done(db, link, out.company, out.added, out.rung, client)
    return await _unresolved(db, link, ctx, out.board_error)


async def _unresolved(db: AsyncSession, link: JobLink, ctx: ResolveContext, board_error: str | None) -> dict:
    """No board: keep the pasted posting on its own if a rung found it, else record why."""
    if ctx.single_job is not None:
        job_id = await _keep_single_job(db, link, ctx.single_job)
        _finish(link, "unresolved", error=SINGLE_ONLY, rung="single_job")
        return {"status": "unresolved", "reason": "single job only", "job_id": job_id, "notes": ctx.notes}
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
