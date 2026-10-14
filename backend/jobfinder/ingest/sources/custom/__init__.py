"""Big-tech custom careers sites, gated on a terms/robots review (see TERMS.md next to this file).

Each candidate site was reviewed (robots.txt + terms of use, 2026-10) before any adapter was written.
`terms_ok=False` sites are refused: they are never registered as an ATS, never recognised by
`board_from_url`, and the resolver ladder stops on them before any rung fetches the page, so a pasted
link from such a site is reported as unsupported with the recorded reason instead of being scraped.

A site whose postings live on an ATS we already read (`board`) is not given its own adapter; a pasted
link from its careers site resolves straight to that ATS board. Adding a custom adapter later means:
review terms, set terms_ok=True, set `ats` and `board=(ats, fixed_slug)`, and register the fetcher in
ingest/sources/__init__.py.

This module is a leaf (no jobfinder imports) so discovery and the resolver can import it without cycles.
"""
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

REVIEWED = "2026-10-14"
_LOCALE_RE = re.compile(r"[a-z]{2}([-_][a-z]{2,4})?|intl|global")


@dataclass(frozen=True)
class CustomSite:
    name: str
    # "host" (the host and its subdomains) or "host/path-prefix"; "name.*" matches any country domain
    # (google.com, google.co.uk); a path prefix may follow up to two locale segments (/intl/en/...)
    hosts: tuple[str, ...]
    terms_ok: bool  # True only when robots.txt AND the terms allow automated, low-volume access
    reason: str  # one line: why it is allowed or refused (shown to the user for refusals)
    ats: str | None = None  # registry key of a custom adapter, when one is built
    board: tuple[str, str] | None = None  # (ats, slug) a pasted link resolves to: the custom adapter's
    # fixed board key, or the already-supported ATS board that holds the site's postings
    reviewed: str = REVIEWED


CUSTOM_SITES: tuple[CustomSite, ...] = (
    CustomSite(
        "Google", ("google.*",), False,
        "robots.txt disallows paging the jobs results (/about/careers/applications/jobs/results?page=) "
        "and Google's terms list scraping content that doesn't belong to you as grounds for suspension.",
    ),
    CustomSite(
        "Apple", ("apple.com",), False,
        "Apple's website Terms of Use forbid any 'page-scrape', 'robot', 'spider' or other automatic "
        "device to access, acquire, copy or monitor any portion of the site.",
    ),
    CustomSite(
        "Amazon", ("amazon.jobs", "amazon.*"), False,
        "amazon.jobs points to Amazon's Conditions of Use, whose licence excludes any use of data "
        "mining, robots, or similar data gathering and extraction tools.",
    ),
    CustomSite(
        "Meta", ("metacareers.com", "facebook.com", "meta.com"), False,
        "metacareers.com/robots.txt states collection of data through automated means is prohibited "
        "without Meta's express written permission.",
    ),
    CustomSite(
        "Tesla", ("tesla.com/careers",), False,
        "tesla.com answers our identified crawler with 403 Access Denied (even robots.txt and the "
        "terms page); we don't evade bot blocking.",
    ),
    CustomSite(
        "IBM", ("ibm.com",), False,
        "IBM's Terms of Use forbid any robot, spider, site search/retrieval application or other "
        "automatic device to retrieve, index or 'data mine' IBM websites.",
    ),
    CustomSite(
        "Uber", ("uber.com/careers",), False,
        "robots.txt disallows */api/, which is where the careers search results are loaded from.",
    ),
    CustomSite(
        "Goldman Sachs", ("higher.gs.com",), False,
        "robots.txt allows only /roles/ and disallows everything else, including the search results.",
    ),
    CustomSite(
        "Citadel", ("citadel.com/careers", "citadelsecurities.com/careers"), False,
        "citadel.com answers our identified crawler with 403 (bot protection); we don't evade it.",
    ),
    CustomSite(
        "Jane Street", ("janestreet.com/join-jane-street",), True,
        "No adapter needed: its postings are a public Greenhouse board (boards-api.greenhouse.io "
        "'janestreet'), so pasted links resolve to that board.",
        board=("greenhouse", "janestreet"),
    ),
    CustomSite(
        "Intel", ("jobs.intel.com",), True,
        "No adapter needed: jobs.intel.com blocks crawlers (403), but its postings are the public "
        "Workday site intel.wd1/External, which we already read.",
        board=("workday", "intel.wd1.External"),
    ),
)


def _split(url: str) -> tuple[str, list[str]] | None:
    """(host, path segments) with percent-escapes decoded and dot segments resolved, so an equivalent
    spelling of a refused path ('/x/../about/c%61reers') still matches."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    segs: list[str] = []
    for seg in unquote(parts.path).lower().split("/"):
        if seg == "..":
            if segs:
                segs.pop()
        elif seg and seg != ".":
            segs.append(seg)
    return host, segs


def _host_matches(phost: str, host: str) -> bool:
    if phost.endswith(".*"):  # brand on any country domain: google.com, google.co.uk, www.google.de
        labels, brand = host.split("."), phost[:-2]
        return any(lab == brand and 1 <= len(labels) - i - 1 <= 2 and all(len(x) <= 3 for x in labels[i + 1:])
                   for i, lab in enumerate(labels))
    return host == phost or host.endswith("." + phost)


def _matches(pattern: str, host: str, segs: list[str]) -> bool:
    phost, _, prefix = pattern.partition("/")
    if not _host_matches(phost, host):
        return False
    if not prefix:
        return True
    want = prefix.lower().split("/")
    for skip in range(3):  # the prefix may follow up to two locale segments
        if not all(_LOCALE_RE.fullmatch(s) for s in segs[:skip]):
            break
        if segs[skip : skip + len(want)] == want:
            return True
    return False


def site_for_url(url: str) -> CustomSite | None:
    """The reviewed custom site a URL belongs to, or None."""
    split = _split(url)
    if split is None:
        return None
    host, segs = split
    for site in CUSTOM_SITES:
        if any(_matches(h, host, segs) for h in site.hosts):
            return site
    return None


def refusal_for_url(url: str) -> CustomSite | None:
    """The site entry when the URL belongs to a site whose terms/robots review refused automated access."""
    site = site_for_url(url)
    return site if site is not None and not site.terms_ok else None


def refusal_message(site: CustomSite) -> str:
    """What the user sees when a pasted link is on a refused site."""
    return f"We don't read {site.name}'s careers site: {site.reason}"


def board_for_url(url: str) -> tuple[str, str] | None:
    """(ats, slug) for a link on an allowed custom site: its own adapter's fixed board, or the supported
    ATS board that holds its postings. None for refused or unknown sites."""
    site = site_for_url(url)
    if site is None or not site.terms_ok:
        return None
    return site.board


