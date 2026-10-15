"""How sought-after each employer is, and which sought-after employers we can't read yet.

Nothing here is a hand-kept list: every input is already in the database. `score_all` gives every company a
`coveted_score` (0-100) from four signals, each normalised to 0..1 and weighted:

  prestige    25  the imported `prestige_tier` (1-5) of seed rows: (tier - 1) / 4. Other rows count only when
                  an admin set a tier (3 is just the column default), otherwise 0.
  lists       25  the board was on a curated outside list the last time discovery read them. Discovery stores
                  no per-board provenance, so its weekly result records every board its curated sources
                  (the GitHub lists) found under `listed`; the latest such result is used. Until discovery has
                  run once with this, the signal is 0 for everyone.
  engagement  20  pastes + saves in the last 90 days: JobLink rows pointing at the company (any status) plus
                  JobMatch rows for its jobs that users saved or applied to. log1p(n) / log1p(max(n_max, 10)),
                  so one paste is worth something but the most-wanted employer sets the top.
  outcomes    20  how well its jobs match and convert across users: 0.6 * mean llm_score / 100 + 0.4 * applied
                  share, shrunk towards 0 by n / (n + 5) so a couple of matches don't count as a trend.

The weights add up to 90: the top tier needs strength on several signals, and an admin pin is the override.
`JobMatch` has no saved-at time, so saves are windowed by when the match was made.

Tiers: 1 if score >= 75 or an admin pinned it (`coveted_pinned is True`), 2 if >= 50, else 3. An admin unpin
(`coveted_pinned is False`) forces score 0, tier 3.

The coverage audit (`audit_coverage`) takes tier 1-2 companies whose board isn't working, plus admin-pinned
employers we have no board for (`CovetedTarget` rows without a company), and sends each through the resolver
ladder at its careers URL (https://{domain}/careers, then careers.{domain}, then jobs.{domain}); the page is
fetched once and handed to the rungs (a redirect to an ATS, an embedded board, JSON-LD, ...). Sites whose terms
review refused us are never fetched. Each target gets one URL family per run, oldest attempt first, and only a
board that is working counts as a fix.
"""
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.config import settings
from jobfinder.core.urls import clean_domain
from jobfinder.ingest.discovery.robots import PoliteFetcher
from jobfinder.ingest.http_safety import TEXT_TYPES, FetchError, safe_fetch
from jobfinder.ingest.resolve.ladder import PAGE_MAX_BYTES
from jobfinder.ingest.sources.custom import refusal_for_url, refusal_message, site_for_url
from jobfinder.models import Company, CovetedTarget, Job, JobLink, JobMatch, Task

log = logging.getLogger("jobfinder.coveted")

WEIGHTS = {"prestige": 25.0, "lists": 25.0, "engagement": 20.0, "outcomes": 20.0}
TIER1, TIER2 = 75.0, 50.0
ENGAGEMENT_DAYS = 90
ENGAGEMENT_FLOOR = 10  # the engagement count that scores full marks when nobody has more
OUTCOME_SHRINK = 5  # matches needed before outcomes count for half their weight
FAILING_DAYS = 14



@dataclass
class Signals:
    prestige: int | None = None  # seed rows only
    listed: bool = False
    engagement: int = 0  # pastes + saves in the window
    matches: int = 0
    mean_llm: float = 0.0
    applied: int = 0


def score(sig: Signals, engagement_max: int = 0) -> float:
    """0-100 from the signals (see the module docstring for the weights)."""
    prestige = (min(max(sig.prestige, 1), 5) - 1) / 4 if sig.prestige else 0.0
    top = max(engagement_max, ENGAGEMENT_FLOOR)
    engagement = min(math.log1p(max(sig.engagement, 0)) / math.log1p(top), 1.0)
    outcomes = 0.0
    if sig.matches > 0:
        raw = 0.6 * min(max(sig.mean_llm, 0), 100) / 100 + 0.4 * min(sig.applied / sig.matches, 1.0)
        outcomes = raw * sig.matches / (sig.matches + OUTCOME_SHRINK)
    total = (WEIGHTS["prestige"] * prestige + WEIGHTS["lists"] * (1.0 if sig.listed else 0.0)
             + WEIGHTS["engagement"] * engagement + WEIGHTS["outcomes"] * outcomes)
    return round(min(max(total, 0.0), 100.0), 1)


def tier_for(value: float, pinned: bool | None) -> int:
    if pinned is False:
        return 3
    if pinned or value >= TIER1:
        return 1
    return 2 if value >= TIER2 else 3


def apply_score(company: Company, value: float) -> None:
    if company.coveted_pinned is False:
        value = 0.0
    company.coveted_score = value
    company.coveted_tier = tier_for(value, company.coveted_pinned)


def board_status(company: Company | None, now: datetime | None = None) -> str:
    """ok | failing | disabled | none (no board at all)."""
    if company is None:
        return "none"
    if not company.enabled or company.recipe_state == "disabled":
        return "disabled"
    now = now or datetime.now(UTC)
    if company.recipe_state == "degraded":
        return "failing"
    if company.consecutive_failures > 0 and (
        company.last_success_at is None or company.last_success_at < now - timedelta(days=FAILING_DAYS)
    ):
        return "failing"
    return "ok"


def is_gap(company: Company, now: datetime | None = None) -> bool:
    """A coveted (tier 1-2) employer whose board we can't read."""
    return (company.coveted_tier or 3) <= 2 and board_status(company, now) != "ok"


async def listed_boards(db: AsyncSession) -> set[tuple[str, str]]:
    """(ats, slug) boards the curated discovery sources found on their last run that recorded them."""
    results = (await db.scalars(
        sa.select(Task.result).where(Task.kind == "discover_boards", Task.status == "done")
        .order_by(Task.id.desc()).limit(10)
    )).all()
    for result in results:
        if isinstance(result, dict) and isinstance(result.get("listed"), list):
            out = set()
            for key in result["listed"]:
                ats, _, slug = str(key).partition("/")
                if ats and slug:
                    out.add((ats, slug.lower()))
            return out
    return set()


async def score_all(db: AsyncSession, now: datetime | None = None) -> dict:
    """Recompute every company's coveted score and tier, and copy them onto linked targets."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=ENGAGEMENT_DAYS)
    listed = await listed_boards(db)
    pastes = dict((await db.execute(
        sa.select(JobLink.company_id, sa.func.count()).where(JobLink.company_id.is_not(None), JobLink.created_at >= since)
        .group_by(JobLink.company_id)
    )).all())
    saves = dict((await db.execute(
        sa.select(Job.company_id, sa.func.count()).join(JobMatch, JobMatch.job_id == Job.id)
        .where(Job.company_id.is_not(None), JobMatch.status.in_(("saved", "applied")), JobMatch.created_at >= since)
        .group_by(Job.company_id)
    )).all())
    outcomes = {
        cid: (n, float(mean or 0), int(applied or 0))
        for cid, n, mean, applied in (await db.execute(
            sa.select(Job.company_id, sa.func.count(), sa.func.avg(JobMatch.llm_score),
                      sa.func.sum(sa.case((JobMatch.status == "applied", 1), else_=0)))
            .join(JobMatch, JobMatch.job_id == Job.id).where(Job.company_id.is_not(None))
            .group_by(Job.company_id)
        )).all()
    }
    engagement = {cid: pastes.get(cid, 0) + saves.get(cid, 0) for cid in set(pastes) | set(saves)}
    top = max(engagement.values(), default=0)
    companies = (await db.scalars(sa.select(Company))).all()
    tiers = {1: 0, 2: 0, 3: 0}
    for c in companies:
        n, mean, applied = outcomes.get(c.id, (0, 0.0, 0))
        sig = Signals(
            # seed rows carry an imported tier; others only one an admin set (3 is just the column default)
            prestige=c.prestige_tier if c.origin == "seed" or c.prestige_tier != 3 else None,
            listed=(c.ats, c.slug.lower()) in listed, engagement=engagement.get(c.id, 0),
            matches=n, mean_llm=mean, applied=applied,
        )
        apply_score(c, score(sig, top))
        tiers[c.coveted_tier] += 1
    by_id = {c.id: c for c in companies}
    for t in (await db.scalars(sa.select(CovetedTarget).where(CovetedTarget.company_id.is_not(None)))).all():
        if (c := by_id.get(t.company_id)) is not None:
            t.score = c.coveted_score
    await db.commit()
    return {"scored": len(companies), "tiers": {str(k): v for k, v in tiers.items()}, "listed": len(listed)}


def guess_domain(company: Company) -> str | None:
    """The employer's own domain, best effort: recipe boards and custom-domain slugs carry it; otherwise the
    name ('Jane Street' -> janestreet.com). Only ever used to try careers URLs, which the resolver validates."""
    from jobfinder.ingest.resolve import registrable_domain

    slug = company.slug or ""
    host = slug.rsplit("__", 1)[-1] if company.ats == "eightfold" else slug.split("__")[0]
    if company.ats not in ("workday", "oracle_hcm") and "." in host:
        d = clean_domain(registrable_domain(f"https://{host}/"))
        if d:
            return d
    word = re.sub(r"[^a-z0-9]", "", (company.name or "").split("(")[0].lower())
    return clean_domain(f"{word}.com") if word else None


def candidate_urls(domain: str) -> list[str]:
    return [f"https://{domain}/careers", f"https://careers.{domain}/", f"https://jobs.{domain}/"]


async def _target_for(db: AsyncSession, company: Company) -> CovetedTarget | None:
    """The target tracking this company's coverage, created on first need (source 'audit')."""
    t = await db.scalar(sa.select(CovetedTarget).where(CovetedTarget.company_id == company.id)
                        .order_by(CovetedTarget.id).limit(1))
    if t is not None:
        return t
    domain = guess_domain(company)
    if domain is None:
        return None
    t = await db.scalar(sa.select(CovetedTarget).where(CovetedTarget.domain == domain))
    if t is not None:
        if t.company_id is None:
            t.company_id = company.id
            await db.commit()
        elif t.company_id != company.id:  # the guessed domain already tracks another company
            return None
        return t
    t = CovetedTarget(name=company.name[:200], domain=domain, source="audit", score=company.coveted_score,
                      company_id=company.id)
    db.add(t)
    try:
        await db.commit()
    except IntegrityError:  # another audit created it first
        await db.rollback()
        t = await db.scalar(sa.select(CovetedTarget).where(CovetedTarget.domain == domain))
    return t


async def resolve_target(db: AsyncSession, target: CovetedTarget, client: httpx.AsyncClient) -> dict:
    """Try the target's careers URLs once each, stopping at the first that leads to a working board."""
    from jobfinder.ingest.resolve import resolve_url

    tid, domain, current = target.id, target.domain, target.company_id
    robots = PoliteFetcher(client)  # shared by every candidate URL, so robots.txt is read once per host
    errors: list[str] = []
    found: Company | None = None
    rung = None
    for url in candidate_urls(domain):
        if (refused := refusal_for_url(url)) is not None:  # terms review said no: never fetched
            errors.append(f"{url}: {refusal_message(refused)}")
            continue
        if site_for_url(url) is not None:  # a reviewed site maps straight to its board, no page fetch
            company, rung, notes = await resolve_url(db, url, client, robots=robots)
        else:
            try:
                page = await safe_fetch(client, url, accept=TEXT_TYPES, max_bytes=PAGE_MAX_BYTES, robots=robots)
            except FetchError as e:
                errors.append(f"{url}: {e}")
                continue
            company, rung, notes = await resolve_url(db, page.url, client, page=page, robots=robots)
        if company is not None and board_status(company) == "ok":
            found = company
            break
        if company is None:
            reason = "; ".join(notes) or "no job board we can read"
        elif company.id == current:
            reason = "still points at the same failing board"
        else:
            reason = f"leads to {company.ats}/{company.slug}, which isn't working either"
        errors.append(f"{url}: {reason}")
    target = await db.get(CovetedTarget, tid)
    if target is None:
        return {"target_id": tid, "skipped": "target deleted"}
    target.last_resolved_at = datetime.now(UTC)
    if found is None:
        target.last_error = " | ".join(errors)[:500] or "no careers page found"
        await db.commit()
        return {"target_id": tid, "domain": domain, "status": "unresolved", "error": target.last_error}
    target.company_id = found.id
    target.last_error = None
    target.score = found.coveted_score
    # an employer an admin added by name is pinned once, when its board is first found (a later unpin sticks)
    if target.source == "admin" and current is None and found.coveted_pinned is None:
        found.coveted_pinned = True
        apply_score(found, found.coveted_score)
    await db.commit()
    return {"target_id": tid, "domain": domain, "status": "resolved", "company_id": found.id, "rung": rung}


async def audit_coverage(db: AsyncSession, limit: int | None = None, client: httpx.AsyncClient | None = None, *,
                         company_id: int | None = None, target_id: int | None = None) -> dict:
    """Send coveted employers with no working board through the resolver (or just the one asked for)."""
    limit = settings.coverage_resolves_per_run if limit is None else limit
    now = datetime.now(UTC)
    if company_id is not None:
        company = await db.get(Company, company_id)
        if company is None:
            return {"skipped": "no such company"}
        target = await _target_for(db, company)
        if target is None:
            return {"skipped": "no domain to try for this company"}
        targets = [target]
    elif target_id is not None:
        target = await db.get(CovetedTarget, target_id)
        if target is None:
            return {"skipped": "no such target"}
        targets = [target]
    else:
        gaps = [c for c in (await db.scalars(sa.select(Company).where(Company.coveted_tier <= 2))).all()
                if is_gap(c, now)]
        gap_ids = [c.id for c in gaps]
        for cid in gap_ids:  # by id: a rollback inside _target_for expires the loaded rows
            if (c := await db.get(Company, cid)) is not None:
                await _target_for(db, c)
        cond = CovetedTarget.company_id.is_(None)
        if gap_ids:
            cond = sa.or_(cond, CovetedTarget.company_id.in_(gap_ids))
        targets = (await db.scalars(
            sa.select(CovetedTarget).where(cond)
            .order_by(CovetedTarget.last_resolved_at.asc().nulls_first(), CovetedTarget.id).limit(max(limit, 0))
        )).all()
    own = client is None
    if client is None:
        from jobfinder.ingest.resolve import _HEADERS

        client = httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_HEADERS)
    results = []
    try:
        for tid in [t.id for t in targets]:  # ids up front: a rollback while resolving one expires the rest
            try:
                if (t := await db.get(CovetedTarget, tid)) is None:
                    continue
                results.append(await resolve_target(db, t, client))
            except Exception as e:  # one bad site must not stop the audit
                log.exception("coverage audit of target %s failed", tid)
                await db.rollback()
                if (t := await db.get(CovetedTarget, tid)) is not None:
                    t.last_resolved_at = datetime.now(UTC)
                    t.last_error = f"Something went wrong while resolving ({type(e).__name__})."
                    await db.commit()
                results.append({"target_id": tid, "status": "error", "error": type(e).__name__})
    finally:
        if own:
            await client.aclose()
    resolved = sum(1 for r in results if r.get("status") == "resolved")
    return {"tried": len(results), "resolved": resolved, "results": results}
