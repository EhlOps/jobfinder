"""Board discovery (REQ-26 a, e): find apply links on public directories, validate and add the boards."""
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.config import get_settings
from jobfinder.ingest.discovery.robots import PoliteFetcher, RobotsDisallowed, make_client
from jobfinder.ingest.validate import SLUG_RE, add_board
from jobfinder.models import Company

log = logging.getLogger("jobfinder.discovery")

URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]\\]+")
_RESERVED = {"www", "api", "app", "apply", "careers", "jobs", "support", "help", "blog", "static", "assets"}
_PATH_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "boards.eu.greenhouse.io": "greenhouse",
    "job-boards.eu.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "careers.smartrecruiters.com": "smartrecruiters",
}
_SUBDOMAIN_SUFFIXES = {".recruitee.com": "recruitee", ".bamboohr.com": "bamboohr"}


def board_from_url(url: str) -> tuple[str, str] | None:
    """Map an apply URL to (ats, slug), or None when it is not a recognised public board."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    ats = _PATH_HOSTS.get(host)
    if ats:
        segs = [s for s in parts.path.split("/") if s]
        slug = segs[0] if segs else ""
        if slug == "embed":  # boards.greenhouse.io/embed/job_app?for=<slug>
            slug = (parse_qs(parts.query).get("for") or [""])[0]
        if slug in _RESERVED or not SLUG_RE.match(slug):
            return None
        return ats, slug
    for suffix, ats in _SUBDOMAIN_SUFFIXES.items():
        if host.endswith(suffix):
            slug = host[: -len(suffix)]
            if "." in slug or slug in _RESERVED or not SLUG_RE.match(slug):
                return None
            return ats, slug
    return None


def boards_from_text(text: str) -> list[tuple[str, str]]:
    """Every distinct (ats, slug) found in any URL in the text, in order of appearance (slug case kept)."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for url in URL_RE.findall(text):
        b = board_from_url(url.rstrip(".,;:"))
        if b:
            out.setdefault((b[0], b[1].lower()), b)
    return list(out.values())


def name_from_slug(slug: str) -> str:
    return re.sub(r"[-_.]+", " ", slug).strip().title() or slug


@dataclass
class Source:
    name: str
    terms_note: str
    terms_ok: bool  # a source is only enabled when its terms allow automated, low-volume access
    fetch: Callable[[PoliteFetcher], Awaitable[list[tuple[str, str]]]] | None = None
    skip_reason: str = ""


@dataclass
class SourceReport:
    name: str
    status: str  # ok | skipped | error
    reason: str = ""
    found: int = 0
    terms: str = ""


@dataclass
class DiscoveryResult:
    sources: list[SourceReport] = field(default_factory=list)
    candidates: int = 0
    already_known: int = 0
    checked: int = 0
    added: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "sources": [r.__dict__ for r in self.sources],
            "candidates": self.candidates,
            "already_known": self.already_known,
            "checked": self.checked,
            "added": self.added,
            "rejected": self.rejected,
        }


def all_sources() -> list[Source]:
    from jobfinder.ingest.discovery import github_lists, hn_hiring, yc

    return [github_lists.SOURCE, hn_hiring.SOURCE, yc.SOURCE, yc.WELLFOUND]


async def discover_boards(
    session: AsyncSession,
    client: httpx.AsyncClient | None = None,
    max_candidates: int | None = None,
    sources: list[Source] | None = None,
) -> DiscoveryResult:
    """Run every enabled source, then validate and add at most max_candidates new boards."""
    cap = get_settings().discovery_max_candidates if max_candidates is None else max_candidates
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
            for ats, slug in boards:
                found.setdefault((ats, slug.lower()), (ats, slug))
        result.candidates = len(found)
        rows = (await session.execute(sa.select(Company.ats, Company.slug))).all()
        known = {(a, s.lower()) for a, s in rows}
        fresh = [b for k, b in found.items() if k not in known]
        result.already_known = len(found) - len(fresh)
        for ats, slug in fresh[: max(cap, 0)]:
            result.checked += 1
            company, reason = await add_board(session, ats, slug, name_from_slug(slug), "discovered", client)
            if company:
                result.added.append(f"{ats}/{slug}")
            else:
                result.rejected[f"{ats}/{slug}"] = reason
    finally:
        if own:
            await client.aclose()
    return result
