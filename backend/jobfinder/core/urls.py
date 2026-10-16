"""URL and hostname helpers.

`clean_domain` turns user input into a bare hostname or None. `public_link` builds an absolute link
into the SPA from PUBLIC_BASE_URL (email links, password reset). `safe_http_url` keeps third-party links
out of an href unless they are plain http(s); `clean_url` is the input hygiene for a pasted job link; and
`registrable_domain` names the site a URL belongs to.
"""
import re
from urllib.parse import urlparse, urlsplit, urlunsplit

# A bare hostname: labels of letters, digits and inner hyphens, at least one dot, a letter-only TLD.
DOMAIN_RE = re.compile(r"^(?=.{4,255}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MAX_URL_CHARS = 2048


class BadLink(ValueError):
    """The pasted text is not a URL we accept. The message is safe to show the user."""


def clean_domain(raw: str) -> str | None:
    """'Acme.COM' -> 'acme.com'; None for anything that isn't a bare hostname (scheme, path, port)."""
    d = (raw or "").strip().lower().rstrip(".")
    d = d.removeprefix("www.")
    return d if DOMAIN_RE.match(d) else None


def public_link(base_url: str, path: str) -> str:
    """public_link("https://jobs.example.com/", "/matches") -> "https://jobs.example.com/matches"."""
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def safe_http_url(url: str | None) -> str:
    """The URL if it is an absolute http(s) link, else "". Job data comes from third parties, so a
    javascript: or data: link must never reach an href."""
    url = (url or "").strip()
    p = urlparse(url)
    return url if p.scheme in ("http", "https") and p.netloc else ""


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


def registrable_domain(url: str) -> str:
    """Best-effort registrable domain ('careers.acme.co.uk' -> 'acme.co.uk'). No public-suffix list, so
    common two-level suffixes are special-cased; good enough for naming and namespacing recipe boards."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "ac", "org", "net", "gov", "edu") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
