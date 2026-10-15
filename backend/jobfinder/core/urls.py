"""URL and hostname helpers.

`clean_domain` turns user input into a bare hostname or None. `public_link` builds an absolute link
into the SPA from PUBLIC_BASE_URL (email links, password reset).
"""
import re

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
