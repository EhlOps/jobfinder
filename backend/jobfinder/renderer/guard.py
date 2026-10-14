"""The renderer's sandbox rules, kept free of Playwright so they can be tested without a browser.

Every request the page makes goes through `Guard.verdict`: only http(s), and only hosts whose every address is
public (the same check as profile/web_page.resolve_public). Recipe selectors are data handed to a fixed
extractor, and `check_selector` keeps them to plain CSS.
"""
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from jobfinder.profile import web_page

ALLOWED_SCHEMES = ("http", "https")
# Resource types the capture never needs: aborting them saves bytes and time on a small arm64 box.
SKIPPED_TYPES = frozenset({"image", "media", "font"})
MAX_SELECTOR_CHARS = 300
_FIELD_ATTR = re.compile(r"@([A-Za-z_][A-Za-z0-9_:.-]*)$")

Resolver = Callable[[str], Awaitable[tuple[str, str]]]


class Blocked(Exception):
    """The page tried to reach something the sandbox refuses. The message is safe to log."""


def scheme_reason(url: str) -> str | None:
    """None if `url` is an http(s) URL with a host, else why not (file:, data:, javascript:, chrome: ...)."""
    try:
        p = urlsplit(url or "")
        host = p.hostname
    except ValueError:
        return "malformed URL"
    scheme = p.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return f"{scheme or 'relative'}: URLs are blocked"
    if not host:
        return "URL has no host"
    return None


class Guard:
    """Per-task request policy. Caches the verdict per host (one DNS lookup per host per task) and keeps the
    running byte count and what was blocked, for the task's result."""

    def __init__(self, resolver: Resolver | None = None, *, max_bytes: int | None = None):
        self._resolver = resolver
        self._hosts: dict[str, str | None] = {}
        self.max_bytes = max_bytes
        self.bytes = 0
        self.blocked: list[str] = []  # "reason: url", bounded
        self.violation: str | None = None  # set when the top-level page itself was refused

    async def verdict(self, url: str) -> str | None:
        """None to allow `url`, else the reason it is refused."""
        reason = scheme_reason(url)
        if reason is None:
            host = (urlsplit(url).hostname or "").lower()
            if host not in self._hosts:
                resolve = self._resolver or web_page.resolve_public
                try:
                    await resolve(url)
                    self._hosts[host] = None
                except web_page.FetchError as e:
                    self._hosts[host] = str(e) or "address refused"
            reason = self._hosts[host]
        if reason and len(self.blocked) < 50:
            self.blocked.append(f"{reason}: {url[:200]}")
        return reason

    def add_bytes(self, n: int) -> bool:
        """Count `n` more bytes; False once the task's budget is spent."""
        self.bytes += n
        return self.max_bytes is None or self.bytes <= self.max_bytes


def check_selector(sel: str | None, *, allow_scope: bool = False) -> str:
    """A recipe's CSS selector, or raise ValueError. It reaches document.querySelectorAll as a string (never
    evaluated); Playwright's own engine syntax (>>, xpath=, text=) and control characters are refused."""
    sel = (sel or "").strip()
    if not sel or len(sel) > MAX_SELECTOR_CHARS:
        raise ValueError("selector is empty or too long")
    if allow_scope and sel == ":scope":
        return sel
    if ">>" in sel or re.search(r"[\x00-\x1f\x7f`{};\\]", sel) or re.match(r"^[a-z_-]+=", sel, re.IGNORECASE):
        raise ValueError(f"unsupported selector {sel[:60]!r}")
    return sel


def check_field(spec: str | None) -> str | None:
    """A field spec: ':scope' (the item's text), '@attr' (an item attribute), 'sel' or 'sel@attr'.
    URL templates ('https://x/{external_id}') are filled by the backend and never sent here."""
    if not spec:
        return None
    m = _FIELD_ATTR.search(spec)
    sel = spec[: m.start()].strip() if m else spec
    if sel:
        check_selector(sel, allow_scope=True)
    return spec
