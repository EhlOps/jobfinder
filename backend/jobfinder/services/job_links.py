"""Pasted job links: a link brings in its company's whole board (for everyone) and shows that board's roles.

The rules: one link per URL per user, a daily limit, refused sites answered at once, known boards
resolved at once (rung 1 is free), and everything else queued for the resolver ladder.
"""
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.config import settings
from jobfinder.core.errors import Invalid, NotFound, RateLimited
from jobfinder.integrations.ats.custom import refusal_for_url, refusal_message
from jobfinder.integrations.directories import board_from_url
from jobfinder.models import JobLink, User
from jobfinder.repositories import job_links as repo
from jobfinder.repositories.job_links import LinkFacts
from jobfinder.schemas.job_links import JobLinkOut, LinkDetail, LinkJob
from jobfinder.services import ratelimit
from jobfinder.services.resolver import BadLink, clean_url, existing_board
from jobfinder.tasks import ledger

RATE_KIND = "job_link"
MAX_JOBS = 200


class LinksOff(NotFound):
    code, message = "job_links_off", "Adding job links is turned off"


class LinkNotFound(NotFound):
    code, message = "link_not_found", "Link not found"


class BadLinkError(Invalid):
    code = "bad_link"


class TooManyLinks(RateLimited):
    code = "too_many_links"


def ensure_enabled() -> None:
    if not settings.job_links_enabled:
        raise LinksOff()


def _out(link: JobLink, f: LinkFacts) -> JobLinkOut:
    return JobLinkOut(
        id=link.id, url=link.url, status=link.status, company_id=link.company_id, company=f.company, ats=link.ats,
        rung=link.rung, error=link.error, job_count=f.job_count, pasted_job_id=f.pasted_job_id,
        created_at=link.created_at, resolved_at=link.resolved_at,
    )


async def _one(db: AsyncSession, link: JobLink) -> JobLinkOut:
    return _out(link, (await repo.facts(db, [link]))[link.id])


async def _own(db: AsyncSession, user: User, link_id: int) -> JobLink:
    link = await repo.get(db, link_id)
    if link is None or link.user_id != user.id:
        raise LinkNotFound()
    return link


async def add(db: AsyncSession, user: User, raw_url: str) -> tuple[JobLinkOut, int]:
    """The link and the status to answer with: 200 already pasted, 201 answered now, 202 queued."""
    ensure_enabled()
    try:
        url = clean_url(raw_url)
    except BadLink as e:
        raise BadLinkError(str(e)) from e

    dup = await repo.by_url(db, user.id, url)
    if dup:
        return await _one(db, dup), 200

    limit = settings.job_links_per_user_day
    if await ratelimit.count_since(db, RATE_KIND, str(user.id), 86400) >= limit:
        raise TooManyLinks(f"You can add up to {limit} job links a day. Try again tomorrow.")
    await ratelimit.hit(db, RATE_KIND, str(user.id))

    refused = refusal_for_url(url)
    if refused is not None:  # terms/robots review said no: answer now, nothing is fetched
        link = await repo.add(db, JobLink(user_id=user.id, url=url, status="unresolved", rung="terms_refused",
                                          error=refusal_message(refused)[:500], resolved_at=sa.func.now()))
        return await _one(db, link), 201

    link = JobLink(user_id=user.id, url=url, status="resolving")
    board = board_from_url(url)
    if board is not None:  # rung 1 is free: answer known boards straight away
        link.ats, link.slug = board
        known = await existing_board(db, *board)
        if known is not None:  # already polled on schedule: nothing to fetch
            link.status, link.company_id, link.rung = "resolved", known.id, "url_pattern"
            link.resolved_at = sa.func.now()
    link = await repo.add(db, link)  # flushed: the task payload needs its id
    if link.status != "resolving":
        return await _one(db, link), 201
    await ledger.add(db, "resolve_job_link", {"link_id": link.id}, user_id=user.id)
    return await _one(db, link), 202


async def list_links(db: AsyncSession, user: User) -> list[JobLinkOut]:
    ensure_enabled()
    links = await repo.for_user(db, user.id)
    facts = await repo.facts(db, links)
    return [_out(link, facts[link.id]) for link in links]


async def detail(db: AsyncSession, user: User, link_id: int) -> LinkDetail:
    ensure_enabled()
    link = await _own(db, user, link_id)
    out = await _one(db, link)
    jobs = [
        LinkJob(id=j.id, title=j.title, location=j.location or "", url=j.url, posted_at=j.posted_at,
                match_id=mid, score=score)
        for j, mid, score in await repo.jobs(db, user.id, link, MAX_JOBS)
    ]
    return LinkDetail(**out.model_dump(), jobs=jobs)


async def delete(db: AsyncSession, user: User, link_id: int) -> None:
    """Removes the user's link only. The board stays: other people may be getting matches from it."""
    ensure_enabled()
    await repo.delete(db, await _own(db, user, link_id))
