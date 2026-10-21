"""Board discovery sources (REQ-26 a, e): find apply links on public directories and map them to (ats, slug)
boards. Fetching and parsing only: services.discovery validates and adds the boards (#53)."""
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from jobfinder.integrations.ats import SLUG_RE
from jobfinder.integrations.ats.eightfold import board_from_url as eightfold_board
from jobfinder.integrations.ats.icims import slug_from_host as icims_slug_from_host
from jobfinder.integrations.ats.oracle_hcm import board_from_ce_url
from jobfinder.integrations.ats.taleo import taleo_board
from jobfinder.integrations.robots import PoliteFetcher
from jobfinder.integrations.site_terms import board_for_url as custom_board_for_url

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


_WD_LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")


def _workday_board(sub: str, path: str) -> tuple[str, str] | None:
    """{tenant}.{wdN}.myworkdayjobs.com/[locale/]{site}/... -> ("workday", "tenant.wdN.site")."""
    bits = sub.split(".")
    if len(bits) != 2 or not re.fullmatch(r"wd\d{1,3}", bits[1]):
        return None
    segs = [s for s in path.split("/") if s]
    if segs and _WD_LOCALE_RE.match(segs[0]):
        segs = segs[1:]
    if not segs or segs[0] in ("wday", "job", "details"):
        return None
    slug = f"{bits[0]}.{bits[1]}.{segs[0]}"
    return ("workday", slug) if SLUG_RE.match(slug) else None


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
    if host.endswith(".myworkdayjobs.com"):
        return _workday_board(host[: -len(".myworkdayjobs.com")], parts.path)
    if host.endswith(".oraclecloud.com"):
        return board_from_ce_url(host, parts.path)
    if host.endswith(".icims.com"):  # {prefix}-{tenant}.icims.com: the whole portal is the board
        slug = icims_slug_from_host(host)
        return ("icims", slug) if slug else None
    if eightfold := eightfold_board(host, parts.path, parts.query):
        return eightfold
    if host.endswith(".taleo.net"):
        return taleo_board(host[: -len(".taleo.net")], parts.path)
    for suffix, ats in _SUBDOMAIN_SUFFIXES.items():
        if host.endswith(suffix):
            slug = host[: -len(suffix)]
            if "." in slug or slug in _RESERVED or not SLUG_RE.match(slug):
                return None
            return ats, slug
    return custom_board_for_url(url)  # terms-reviewed custom careers sites (refused ones -> None)


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
    curated: bool = False  # a hand-curated list: being on it counts towards the coveted score (services.coverage)


class FoundBoards(list):
    """A source's boards plus a warning for each input it had to skip."""

    def __init__(self, boards=(), warnings: list[str] | None = None):
        super().__init__(boards)
        self.warnings = warnings or []


def all_sources() -> list[Source]:
    return [github_lists.SOURCE, hn_hiring.SOURCE, yc.SOURCE, yc.WELLFOUND]


# The directory modules build their Source from the names above, so they load last.
from jobfinder.integrations.directories import github_lists, hn_hiring, yc
