"""Board discovery (REQ-26 a, e): run the directory sources (integrations.directories), then validate and add the
new boards they found. Never commits: the discover_boards task's `checkpoint` does, after each board (#53)."""
import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.config import settings
from jobfinder.integrations.directories import Source, all_sources, name_from_slug
from jobfinder.integrations.robots import PoliteFetcher, RobotsDisallowed, make_client
from jobfinder.models import Company
from jobfinder.services.board_validation import ValidationResult, add_board, validate_board

log = logging.getLogger("jobfinder.discovery")


class RobotsBlocked(httpx.HTTPError):
    """robots.txt forbids a validation probe request."""


class _PoliteClient:
    """Duck-types the slice of httpx.AsyncClient the ATS fetchers use, routing each GET through robots.txt."""

    def __init__(self, fetcher: PoliteFetcher):
        self._fetcher = fetcher

    async def get(self, url: str, **kwargs) -> httpx.Response:
        try:
            return await self._fetcher.get(url, **kwargs)
        except RobotsDisallowed as e:
            raise RobotsBlocked(str(e)) from e

    async def post(self, url: str, **kwargs) -> httpx.Response:
        try:
            return await self._fetcher.post(url, **kwargs)
        except RobotsDisallowed as e:
            raise RobotsBlocked(str(e)) from e


async def validate_candidates(
    fetcher: PoliteFetcher, boards: list[tuple[str, str]], concurrency: int | None = None
) -> list[ValidationResult]:
    """Probe boards concurrently (bounded), list-only and robots-checked; results are in input order."""
    sem = asyncio.Semaphore(max(concurrency or settings.ingest_concurrency, 1))
    probe_client = _PoliteClient(fetcher)

    async def one(ats: str, slug: str) -> ValidationResult:
        async with sem:
            return await validate_board(ats, slug, probe_client, list_only=True)  # type: ignore[arg-type]

    return list(await asyncio.gather(*(one(a, s) for a, s in boards)))


@dataclass
class SourceReport:
    name: str
    status: str  # ok | partial (some inputs failed, see warnings) | skipped | error
    reason: str = ""
    found: int = 0
    terms: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class DiscoveryResult:
    sources: list[SourceReport] = field(default_factory=list)
    candidates: int = 0
    already_known: int = 0
    checked: int = 0
    added: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    listed: set[str] = field(default_factory=set)  # "ats/slug" (lowercase) found on curated sources

    def as_dict(self) -> dict:
        return {
            "sources": [r.__dict__ for r in self.sources],
            "candidates": self.candidates,
            "already_known": self.already_known,
            "checked": self.checked,
            "added": self.added,
            "rejected": self.rejected,
            "listed": sorted(self.listed),
        }


async def discover_boards(
    session: AsyncSession,
    client: httpx.AsyncClient | None = None,
    max_candidates: int | None = None,
    sources: list[Source] | None = None,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
) -> DiscoveryResult:
    """Run every enabled source, then validate and add at most max_candidates new boards.
    `checkpoint` (the task's commit) runs after each board is added, so one failure keeps the earlier ones."""
    cap = settings.discovery_max_candidates if max_candidates is None else max_candidates
    own = client is None
    client = client or make_client()
    result = DiscoveryResult()
    found: dict[tuple[str, str], tuple[str, str]] = {}
    try:
        fetcher = PoliteFetcher(client)
        for src in sources if sources is not None else all_sources():
            rep = SourceReport(src.name, "ok", terms=src.terms_note)
            result.sources.append(rep)
            if not src.terms_ok or src.fetch is None:
                rep.status, rep.reason = "skipped", src.skip_reason or "terms do not allow automated access"
                continue
            try:
                boards = await src.fetch(fetcher)
            except RobotsDisallowed as e:
                rep.status, rep.reason = "skipped", str(e)
                continue
            except Exception as e:
                log.warning("discovery source %s failed: %s", src.name, e)
                rep.status, rep.reason = "error", f"{type(e).__name__}: {e}"[:300]
                continue
            rep.found = len(boards)
            rep.warnings = list(getattr(boards, "warnings", []))
            if rep.warnings:
                rep.status = "partial"
            for ats, slug in boards:
                found.setdefault((ats, slug.lower()), (ats, slug))
                if src.curated:
                    result.listed.add(f"{ats}/{slug.lower()}")
        result.candidates = len(found)
        rows = (await session.execute(sa.select(Company.ats, Company.slug))).all()
        known = {(a, s.lower()) for a, s in rows}
        fresh = [b for k, b in found.items() if k not in known]
        result.already_known = len(found) - len(fresh)
        if len(fresh) > cap:
            # rejected boards are not remembered, so a fixed order would re-probe the same dead ones every week
            random.shuffle(fresh)
        batch = fresh[: max(cap, 0)]
        validations = await validate_candidates(fetcher, batch)
        for (ats, slug), validation in zip(batch, validations, strict=True):
            result.checked += 1
            company, reason = await add_board(
                session, ats, slug, name_from_slug(slug), "discovered", client, validation=validation
            )
            if company:
                result.added.append(f"{ats}/{slug}")
                if checkpoint is not None:
                    await checkpoint()
            else:
                result.rejected[f"{ats}/{slug}"] = reason
    finally:
        if own:
            await client.aclose()
    return result
