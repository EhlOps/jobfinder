"""The sandboxed headless browser (rung 4.5). Only the renderer container imports this; Playwright is an optional
extra (`renderer`) and is imported lazily so the rest of the package never needs it.

Per task: a fresh browser context (no cookies or storage carried over), downloads off, service workers
blocked, WebSockets never connected, popups and dialogs closed. Chromium is launched behind a dead proxy, so it
has no network of its own: one route handler answers every request the page makes, after refusing non-http(s)
URLs and non-public addresses (guard.Guard), by fetching it from Python through net.PinnedTransport (each hop
pinned to its checked IP, redirects handled here hop by hop, bodies size-capped). A hard per-task timeout and a
total-bytes budget bound the work. The browser never runs JavaScript we were given: selectors from a recipe are
plain strings passed as arguments to one fixed extractor function below.
"""
import asyncio
import contextlib
import json
import logging
from urllib.parse import urljoin, urlsplit

import httpx

from jobfinder.profile.web_page import FetchError, resolve_public
from jobfinder.renderer import net
from jobfinder.renderer.guard import (
    SKIPPED_TYPES,
    Blocked,
    Guard,
    check_field,
    check_selector,
    scheme_reason,
)

log = logging.getLogger("jobfinder.renderer")

TASK_TIMEOUT_S = 30.0
NAV_TIMEOUT_MS = 20_000
ITEMS_WAIT_MS = 8_000  # render_list: how long to wait for the first item to appear
SETTLE_MS = 6_000  # after DOMContentLoaded, wait at most this long for the network to go quiet
REQUEST_TIMEOUT_S = 15.0
MAX_REDIRECTS = 4
MAX_TOTAL_BYTES = 40_000_000  # SPA bundles alone run to ~20 MB
MAX_RESPONSE_BYTES = 15_000_000
MAX_JSON_RESPONSES = 20
BODY_PREVIEW_BYTES = 200_000
REQUEST_BODY_MAX = 20_000
HTML_MAX_BYTES = 2_000_000
MAX_LIST_ITEMS = 2000
MAX_FIELD_CHARS = 20_000
BROWSER_UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36 JobFinder/0.1 (+personal job search)")
DEAD_PROXY = "http://127.0.0.1:9"  # discard port: anything Chromium sends on its own goes nowhere
LAUNCH_ARGS = [
    f"--proxy-server={DEAD_PROXY}", "--proxy-bypass-list=<-loopback>",  # loopback through the proxy too
    "--dns-prefetch-disable", "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--disable-background-networking", "--disable-sync", "--disable-default-apps",
    "--mute-audio", "--disable-features=Translate,MediaRouter,OptimizationHints,NetworkPrediction",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",  # no WebRTC path around the proxy
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
]
JSON_MARKERS = ("application/json", "+json", "text/json")

# The one script the renderer evaluates. Recipe selectors arrive as data (querySelectorAll strings).
_EXTRACT_JS = """
([itemSel, fields, max, maxChars]) => {
  const attrRe = /@([A-Za-z_][A-Za-z0-9_:.-]*)$/;
  const value = (el, spec) => {
    if (!spec) return null;
    let sel = spec, attr = null;
    const m = spec.match(attrRe);
    if (m) { attr = m[1]; sel = spec.slice(0, m.index).trim(); }
    const node = (!sel || sel === ':scope') ? el : el.querySelector(sel);
    if (!node) return null;
    if (attr) {
      const raw = node.getAttribute(attr);
      if (raw === null) return null;
      if (attr === 'href' || attr === 'src') {
        try { return new URL(raw, document.baseURI).href; } catch (e) { return raw; }
      }
      return raw;
    }
    return (node.innerText || node.textContent || '').trim();
  };
  const out = [];
  for (const el of Array.from(document.querySelectorAll(itemSel)).slice(0, max)) {
    const item = {};
    for (const [k, spec] of Object.entries(fields)) {
      const v = value(el, spec);
      item[k] = v === null ? null : String(v).slice(0, maxChars);
    }
    out.push(item);
  }
  return out;
}
"""


class Renderer:
    """One Chromium for the process, relaunched if it dies. Each task gets its own context."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def browser(self):
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                from playwright.async_api import async_playwright

                if self._pw is None:
                    self._pw = await async_playwright().start()
                # chromium_sandbox stays off: Docker's default seccomp profile (and cap_drop ALL) leave
                # Chromium no namespace sandbox to use, so the container is the sandbox.
                self._browser = await self._pw.chromium.launch(headless=True, args=LAUNCH_ARGS)
            return self._browser

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            if self._browser is not None:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw is not None:
                await self._pw.stop()
        self._browser = self._pw = None


def _is_json(ctype: str) -> bool:
    ctype = (ctype or "").split(";")[0].strip().lower()
    return any(m in ctype for m in JSON_MARKERS)


def _clean(text: str) -> str:
    """Postgres JSONB can't store NUL characters."""
    return text.replace("\x00", "")


class _Session:
    """Answers one context's requests through the guard and the pinned client; records JSON responses."""

    def __init__(self, guard: Guard, http: httpx.AsyncClient, *, capture: bool):
        self.guard = guard
        self.http = http
        self.capture = capture
        self.json_responses: list[dict] = []
        self.page = None
        self.context = None
        self.over_budget = False
        self.redirect_to: str | None = None  # a checked redirect of the top-level page, for _load to follow
        self._seen: set[tuple] = set()

    async def handle(self, route) -> None:
        req = route.request
        try:
            if req.resource_type in SKIPPED_TYPES or self.over_budget:
                await route.abort("blockedbyclient")
                return
            top = self._is_top_navigation(req)
            reason = await self.guard.verdict(req.url)
            if reason:
                if top:
                    self.guard.violation = reason
                await route.abort("blockedbyclient")
                return
            answer = await self._fetch(req, top)
            if answer is None:
                await route.abort("blockedbyclient" if self.guard.violation or self.over_budget else "failed")
                return
            url, resp, body = answer
            if self.capture:
                self._record(req, resp, body, url)
            await route.fulfill(status=resp.status_code, headers=net.response_headers(resp), body=body)
        except Exception as e:  # never leave a request hanging; a closed page also lands here
            log.debug("route %s failed: %s", req.url[:200], e)
            with contextlib.suppress(Exception):
                await route.abort("failed")

    async def _fetch(self, req, top: bool) -> tuple[str, httpx.Response, bytes] | None:
        """(final url, response, body) for the request, or None to abort it."""
        method, url = req.method, req.url
        # req.headers, not all_headers(): raw headers only exist once Chromium sends the request, which it
        # never does here. Cookies come from the context's own (per-task) jar instead.
        headers = net.request_headers(dict(req.headers))
        if self.context is not None:
            jar = await self.context.cookies([url])
            if jar:
                headers["cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in jar)
        content = req.post_data_buffer
        for _hop in range(MAX_REDIRECTS + 1):
            budget = min(MAX_RESPONSE_BYTES, (self.guard.max_bytes or MAX_TOTAL_BYTES) - self.guard.bytes)
            try:
                async with self.http.stream(method, url, headers=headers, content=content,
                                            timeout=REQUEST_TIMEOUT_S) as resp:
                    location = resp.headers.get("location") if resp.is_redirect else None
                    body = b"" if location else await net.read_capped(resp, max(budget, 0))
            except net.TooLarge:
                if budget < MAX_RESPONSE_BYTES:
                    self.over_budget = True
                return None
            except httpx.HTTPError as e:
                log.debug("fetch %s failed: %s", url[:200], e)
                return None
            if not location:
                self.guard.add_bytes(len(body))
                return url, resp, body
            target = urljoin(url, location)
            reason = await self.guard.verdict(target)
            if reason:  # a redirect towards something internal: refuse, and stop if it was the page
                if top:
                    self.guard.violation = reason
                return None
            if top:  # _load navigates there itself, so the page's URL (and its relative links) stay right
                self.redirect_to = target
                return None
            if resp.status_code == 303 or (resp.status_code in (301, 302) and method == "POST"):
                method, content = "GET", None
            url = target
        return None

    def _is_top_navigation(self, req) -> bool:
        try:
            return req.is_navigation_request() and self.page is not None and req.frame == self.page.main_frame
        except Exception:
            return False

    def _record(self, req, resp: httpx.Response, body: bytes, url: str) -> None:
        if len(self.json_responses) >= MAX_JSON_RESPONSES or req.resource_type not in ("xhr", "fetch"):
            return
        ctype = resp.headers.get("content-type", "")
        if resp.status_code >= 400 or not _is_json(ctype) or not body:
            return
        post = req.post_data or None
        key = (req.method, url, post)
        if key in self._seen:  # SPAs re-request the same thing; keep the slots for distinct endpoints
            return
        self._seen.add(key)
        preview = body[:BODY_PREVIEW_BYTES].decode("utf-8", errors="replace")
        self.json_responses.append({
            "url": url, "method": req.method, "status": resp.status_code,
            "content_type": ctype.split(";")[0].strip().lower(), "body_preview": _clean(preview),
            "truncated": len(body) > BODY_PREVIEW_BYTES, "size": len(body),
            "request_body": _clean(post[:REQUEST_BODY_MAX]) if post else None,
        })

    def on_frame(self, frame) -> None:
        """A main frame that ends up on a non-http(s) document (data:, file:, chrome:, blob:) stops the task."""
        url = frame.url or ""
        if url in ("", "about:blank") or self.page is None:
            return
        if frame == self.page.main_frame and scheme_reason(url):
            self.guard.violation = scheme_reason(url)


async def _no_websocket(ws) -> None:
    with contextlib.suppress(Exception):
        await ws.close()


async def check_top_url(url: str, client: httpx.AsyncClient | None = None) -> None:
    """The top-level URL must be public http(s) and allowed by robots.txt. Raises Blocked."""
    reason = scheme_reason(url)
    if reason:
        raise Blocked(reason)
    try:
        await resolve_public(url)
    except FetchError as e:
        raise Blocked(str(e)) from None
    own = client is None
    client = client or net.make_client(timeout=10)
    try:
        if not await net.robots_allowed(client, url):
            raise Blocked(f"robots.txt disallows {url}")
    finally:
        if own:
            await client.aclose()


@contextlib.asynccontextmanager
async def _open(renderer: Renderer, session: _Session):
    browser = await renderer.browser()
    context = await browser.new_context(
        accept_downloads=False, service_workers="block", user_agent=BROWSER_UA, locale="en-US",
        viewport={"width": 1280, "height": 900}, permissions=[], ignore_https_errors=False,
    )
    try:
        await context.route("**/*", session.handle)
        if hasattr(context, "route_web_socket"):
            await context.route_web_socket("**/*", _no_websocket)
        page = await context.new_page()
        session.page, session.context = page, context
        context.on("page", lambda p: p is not page and asyncio.ensure_future(p.close()))
        page.on("framenavigated", session.on_frame)
        page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
        yield page
    finally:
        with contextlib.suppress(Exception):
            await context.close()


async def _load(page, session: _Session, url: str) -> None:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    target = url
    for _hop in range(MAX_REDIRECTS + 1):
        if target != url:  # a redirect (already checked by the route guard): same rules as the first URL
            await check_top_url(target, session.http)
        session.redirect_to = None
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightError as e:
            if session.guard.violation:
                raise Blocked(f"page refused: {session.guard.violation}") from None
            if session.redirect_to:
                target = session.redirect_to
                continue
            raise FetchError(f"page did not load: {str(e).splitlines()[0][:200]}") from None
        with contextlib.suppress(PlaywrightTimeout):
            await page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
        if session.guard.violation:
            raise Blocked(f"page refused: {session.guard.violation}")
        if not session.redirect_to:  # else a script navigation was redirected: follow it the same way
            break
        target = session.redirect_to
    else:
        raise FetchError("too many redirects")
    reason = scheme_reason(page.url) or await session.guard.verdict(page.url)
    if reason:
        raise Blocked(f"page ended up somewhere refused: {reason}")


@contextlib.asynccontextmanager
async def _task(renderer: Renderer, url: str, *, capture: bool, http: httpx.AsyncClient | None):
    own = http is None
    http = http or net.make_client()
    try:
        await check_top_url(url, http)
        session = _Session(Guard(max_bytes=MAX_TOTAL_BYTES), http, capture=capture)
        try:
            async with asyncio.timeout(TASK_TIMEOUT_S), _open(renderer, session) as page:
                await _load(page, session, url)
                yield session, page
        except TimeoutError:
            raise FetchError(f"render timed out after {TASK_TIMEOUT_S:.0f}s") from None
    finally:
        if own:
            await http.aclose()


async def render_capture(renderer: Renderer, url: str, *, http: httpx.AsyncClient | None = None) -> dict:
    """Load `url` and return {final_url, json_responses: [...], html, bytes, blocked}."""
    async with _task(renderer, url, capture=True, http=http) as (session, page):
        html = await page.content()
        final_url = page.url
    html_bytes = html.encode("utf-8", errors="replace")[:HTML_MAX_BYTES]
    return {
        "final_url": final_url, "json_responses": session.json_responses,
        "html": _clean(html_bytes.decode("utf-8", errors="ignore")), "bytes": session.guard.bytes,
        "over_budget": session.over_budget, "blocked": session.guard.blocked[:20],
    }


def list_spec(recipe: dict) -> tuple[str, str, dict[str, str]]:
    """(url, items selector, {field: spec}) of a rendered_html recipe, checked. Raises ValueError.
    URL templates are left out: the backend fills those itself."""
    from jobfinder.ingest.recipes import RecipeError, parse_recipe

    try:
        r = parse_recipe(recipe)
    except RecipeError as e:
        raise ValueError(str(e)) from None
    if r.kind != "rendered_html":
        raise ValueError(f"not a rendered_html recipe: {r.kind}")
    fields = {}
    for name, spec in r.fields.model_dump().items():
        if spec and "{" not in spec:
            fields[name] = check_field(spec)
    return r.list.url, check_selector(r.items), fields


async def render_list(renderer: Renderer, recipe: dict, *, http: httpx.AsyncClient | None = None) -> dict:
    """Run a rendered_html recipe: render its page and read each item's fields. {final_url, items}."""
    url, items_sel, fields = list_spec(recipe)
    async with _task(renderer, url, capture=False, http=http) as (session, page):
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        with contextlib.suppress(PlaywrightTimeout):  # SPAs often fill the list after the network settles
            await page.wait_for_selector(f"css={items_sel}", state="attached", timeout=ITEMS_WAIT_MS)
        items = await page.evaluate(_EXTRACT_JS, [items_sel, fields, MAX_LIST_ITEMS, MAX_FIELD_CHARS])
        final_url = page.url
    if not isinstance(items, list):
        items = []
    clean = [{k: (_clean(v) if isinstance(v, str) else None) for k, v in it.items() if k in fields}
             for it in items if isinstance(it, dict)]
    return {"final_url": final_url, "items": clean[:MAX_LIST_ITEMS], "bytes": session.guard.bytes}


def summary(result: dict) -> str:
    """A short human-readable view of a capture, for the CLI."""
    lines = [f"final_url: {result.get('final_url')}", f"bytes: {result.get('bytes')}",
             f"html: {len(result.get('html') or '')} chars", "json responses:"]
    for r in result.get("json_responses") or []:
        host = urlsplit(r["url"]).hostname
        lines.append(f"  {r['method']} {r['status']} {r['size']:>8}B {host} {r['url'][:160]}")
    if result.get("blocked"):
        lines.append("blocked:")
        lines.extend(f"  {b}" for b in result["blocked"][:10])
    return "\n".join(lines)


def dumps(result: dict) -> str:
    return json.dumps(result, default=str)
