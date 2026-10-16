"""URL and hostname helpers.

`clean_domain` turns user input into a bare hostname or None. `public_link` builds an absolute link
into the SPA from PUBLIC_BASE_URL (email links, password reset). `safe_http_url` keeps only absolute
http(s) links, for third-party URLs that end up in an href.
"""
import re
from urllib.parse import urlparse

# A bare hostname: labels of letters, digits and inner hyphens, at least one dot, a letter-only TLD.
DOMAIN_RE = re.compile(r"^(?=.{4,255}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


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
