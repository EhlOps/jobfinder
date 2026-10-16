"""Rung 2: the page behind the link is, embeds or points at a known ATS board.

In order: the final URL after redirects (a careers link that redirects to the ATS), a custom-domain platform
recognised from the HTML (ats/detect.py: Phenom, Oracle CE, SuccessFactors, ...), then every board URL the
HTML mentions (href, iframe/script src, data-* attributes, inline JS and JSON) plus the embed snippets that
only carry a token (Grnhse.Settings.boardToken, leverJobsOptions.accountName). Nothing else is fetched here.
"""
import html as htmllib
import re
from collections import Counter

from jobfinder.core.urls import registrable_domain
from jobfinder.ingest.resolve.ladder import BoardHit, ResolveContext
from jobfinder.ingest.validate import SLUG_RE
from jobfinder.integrations.ats.detect import detect_board
from jobfinder.integrations.directories import URL_RE, board_from_url

MAX_SCAN_CHARS = 3_000_000
MAX_REFS = 5_000  # board references counted per page
MAX_DISTINCT = 5  # more unrelated boards than this and the page is a list of companies, not one company's

# JSON-escaped slashes ("https:\/\/jobs.lever.co\/acme") and protocol-relative embeds ("//boards.greenhouse.io")
_ESCAPED_SLASH_RE = re.compile(r"\\/|\\u002[fF]")
_PROTO_REL_RE = re.compile(r"(?<![:\w/])//(?=[a-z0-9-]+(?:\.[a-z0-9-]+)+/)", re.IGNORECASE)
_TOKEN_RES = [
    ("greenhouse", re.compile(r"""boardToken["']?\s*[:=]\s*["']([A-Za-z0-9._-]{1,200})["']""")),
    ("lever", re.compile(r"""leverJobsOptions\s*=\s*\{[^}]{0,500}?accountName["']?\s*:\s*["']([A-Za-z0-9._-]{1,200})["']""")),
]
# board APIs a page may call directly from its own JS
_API_RES = [
    ("greenhouse", re.compile(r"^https?://boards-api\.greenhouse\.io/v1/boards/([^/?#]+)", re.IGNORECASE)),
    ("lever", re.compile(r"^https?://api(?:\.eu)?\.lever\.co/v0/postings/([^/?#]+)", re.IGNORECASE)),
    ("ashby", re.compile(r"^https?://api\.ashbyhq\.com/posting-api/job-board/([^/?#]+)", re.IGNORECASE)),
    ("workday", re.compile(r"^https?://([a-z0-9-]+)\.(wd\d{1,3})\.myworkdayjobs\.com/wday/cxs/[^/]+/([^/?#]+)",
                           re.IGNORECASE)),
]


def _api_board(url: str) -> tuple[str, str] | None:
    for ats, pat in _API_RES:
        m = pat.match(url)
        if m:
            groups = list(m.groups())
            if ats == "workday":  # the host part is case-insensitive; board_from_url lowercases it too
                groups[0], groups[1] = groups[0].lower(), groups[1].lower()
            slug = ".".join(groups)
            return (ats, slug) if SLUG_RE.match(slug) else None
    return None


def page_boards(html: str) -> Counter:
    """(ats, slug) -> how often the HTML references it. Keys keep the slug's first-seen case; insertion
    order is first appearance."""
    text = htmllib.unescape(html[:MAX_SCAN_CHARS])
    text = _PROTO_REL_RE.sub("https://", _ESCAPED_SLASH_RE.sub("/", text))
    counts: Counter = Counter()
    seen: dict[tuple[str, str], tuple[str, str]] = {}

    def add(board: tuple[str, str] | None) -> None:
        if board:
            counts[seen.setdefault((board[0], board[1].lower()), board)] += 1

    for m in URL_RE.finditer(text):
        if counts.total() >= MAX_REFS:
            break
        url = m.group(0).rstrip(".,;:")
        add(board_from_url(url) or _api_board(url))
    for ats, pat in _TOKEN_RES:
        for m in pat.finditer(text):
            if SLUG_RE.match(m.group(1)):
                add((ats, m.group(1)))
    return counts


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _resembles(slug: str, domain_label: str) -> bool:
    """Does the board slug look like the company's domain ('acme-robotics' ~ 'acmerobotics.com')?"""
    head = _norm(slug.split(".")[0])  # workday "tenant.wd5.site" -> tenant
    label = _norm(domain_label)
    if not head or len(label) < 3:
        return False
    return head == label or (len(head) >= 3 and (head in label or label in head))


def pick_board(counts: Counter, page_url: str) -> tuple[str, str] | None:
    """The board whose slug resembles the page's domain, else the most-referenced one if it clearly
    dominates (referenced at least twice and more than any other). None for a page that merely mentions
    other companies' boards (a news article, an aggregator or a blog post listing companies)."""
    if not counts:
        return None
    label = registrable_domain(page_url).split(".")[0]
    for board in counts:
        if _resembles(board[1], label):
            return board
    if len(counts) > MAX_DISTINCT:
        return None
    ranked = counts.most_common(2)
    top, n = ranked[0]
    if n < 2 or (len(ranked) > 1 and ranked[1][1] == n):
        return None
    return top


class EmbeddedRung:
    name = "embedded"
    order = 2.0

    async def run(self, ctx: ResolveContext) -> BoardHit | None:
        page = await ctx.page()
        if page is None:
            return None
        final = board_from_url(page.url)
        if final:  # the careers link redirected to the ATS
            return BoardHit(*final)
        html = page.text
        found = detect_board(page.url, html)
        if found:
            return BoardHit(*found)
        counts = page_boards(html)
        board = pick_board(counts, page.url)
        if board is None:
            if counts:
                ctx.note(f"embedded: {len(counts)} unrelated boards referenced; none picked")
            return None
        return BoardHit(*board)
