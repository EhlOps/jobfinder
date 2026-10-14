"""Rung 4: listing sniff. Find the company's own job listing from the pasted page and turn it into a recipe.

Candidates, cheapest first, all on the pasted URL's registrable domain and within MAX_FETCHES requests:
  (a) JSON endpoints the page (or a listing page) references -> the largest array of job-like objects
      becomes a json_api recipe with auto-detected field paths;
  (b) listing pages (the job URL's parent, /careers, /jobs, links on the page, sitemap entries) whose
      repeated anchors match the pasted job's URL pattern -> an html recipe (title = anchor text).
A candidate only wins if running it returns the pasted job (validate_recipe); every rejection is noted.
"""
import html as htmllib
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urljoin, urlsplit

from jobfinder.ingest.base import safe_http_url
from jobfinder.ingest.http_safety import (
    JSON_TYPES,
    TEXT_TYPES,
    FetchError,
    SafeResponse,
    safe_fetch,
)
from jobfinder.ingest.recipes import Recipe, RecipeError, parse_recipe, run_recipe, validate_recipe
from jobfinder.ingest.recipes.html import compile_selector, page_text, parse_document, select
from jobfinder.ingest.recipes.sniff import explain_json_sample
from jobfinder.ingest.resolve.ladder import PAGE_MAX_BYTES, RecipeHit, ResolveContext

MAX_FETCHES = 8  # per resolve: candidate fetches plus validation runs
MAX_JSON_PER_PAGE = 3
MAX_VALIDATE_PAGES = 3
MIN_JOB_LINKS = 2
MAX_SITEMAP_LOCS = 20_000
MAX_SAMPLE_CHARS = 8_000  # JSON kept on ctx.json_samples per endpoint

_QUOTED_URL = re.compile(r"""(href\s*=\s*)?["'](https?://[^"'\s<>\\]{4,500}|/[^"'\s<>\\]{1,500})["']""",
                         re.IGNORECASE)
_APIISH = re.compile(r"/api/|\.json(?:$|\?)|/api\b|//api\.", re.IGNORECASE)
_JOBSISH = re.compile(r"jobs|positions|careers|openings|vacanc|requisition", re.IGNORECASE)
_STATIC = re.compile(r"\.(?:m?js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|webp|mp4|pdf|map|xml)(?:$|\?)",
                     re.IGNORECASE)
_LISTING_PATH = re.compile(r"/(?:careers?|jobs|job-search|search-jobs|openings|open-positions|positions|"
                           r"vacancies|join-us|work-with-us)/?$", re.IGNORECASE)
_LOC = re.compile(r"<loc>\s*([^<\s]{1,2048})\s*</loc>", re.IGNORECASE)
_GENERIC_TAIL = {"apply", "view", "details", "detail", "description", "overview", ""}
_SAFE_VALUE = re.compile(r"[^\"'\\\]\x00-\x20]{2,200}")
_CLASS = re.compile(r"-?[A-Za-z_][A-Za-z0-9_-]*")


def job_pattern(url: str) -> str | None:
    """The part of a job URL that every job on the site shares: '/jobs/4021' -> '/jobs/',
    '/careers/job/123/apply' -> '/careers/job/', '/jobs/4021/senior-eng' -> '/jobs/', '/job?id=7' -> '/job?id='.
    The job's own segment is the last one with a digit in it (else the last one)."""
    p = urlsplit(url)
    segs = p.path.split("/")[1:]
    while segs and segs[-1].lower() in _GENERIC_TAIL:
        segs.pop()
    own = next((i for i in range(len(segs) - 1, -1, -1) if re.search(r"\d", segs[i])), len(segs) - 1)
    if own >= 1:
        pattern = "/" + "/".join(segs[:own]) + "/"
    else:
        key = next((k for k, v in parse_qsl(p.query) if re.fullmatch(r"[\w-]{2,}", v) and re.search(r"\d", v)),
                   None)
        pattern = f"{p.path}?{key}=" if key and p.path else None
    return pattern if pattern and _SAFE_VALUE.fullmatch(pattern) else None


def _shape(rel: str, pattern: str, *, strip_tail: bool = False) -> tuple[int, str] | None:
    """How a job link continues after the shared pattern: (segments, 'num' | 'mixed' | 'word'), so
    '/jobs/8174/' and '/jobs/8139/' agree while '/jobs/location/boston/' doesn't. strip_tail ignores a
    trailing '/apply' etc. (for the pasted link; on a listing, '/jobs/8174/apply' is a different link)."""
    if pattern not in rel:
        return None
    rest = rel.split(pattern, 1)[1]
    rest = rest.split("&")[0] if "?" in pattern else rest.split("?")[0]
    segs = [x for x in rest.split("/") if x]
    while strip_tail and segs and segs[-1].lower() in _GENERIC_TAIL:
        segs.pop()
    if not segs:
        return None
    first = segs[0]
    return len(segs), "num" if first.isdigit() else "mixed" if re.search(r"\d", first) else "word"


def _rel(url: str) -> str:
    u = urlsplit(url)
    return u.path + (f"?{u.query}" if u.query else "")


def _selectors(anchors: list, base: str):
    """Candidate item selectors for these anchors, most specific first: a shared class on the anchors, then
    a shared ancestor (tag + class, up to 4 levels up), then any anchor with the pattern."""
    classes = set.intersection(*(set((a.get("class") or "").split()) for a in anchors))
    for c in sorted(x for x in classes if _CLASS.fullmatch(x)):
        yield f"a.{c}{base}"
    for depth in range(1, 5):
        ancs = []
        for a in anchors:
            el = a
            for _ in range(depth):
                el = el.getparent() if el is not None else None
            ancs.append(el)
        if any(el is None or not isinstance(el.tag, str) for el in ancs) or len({el.tag for el in ancs}) != 1:
            break
        tag = ancs[0].tag
        if not _CLASS.fullmatch(tag) or tag in ("html", "body"):
            break
        comb = " > " if depth == 1 else " "
        shared = set.intersection(*(set((el.get("class") or "").split()) for el in ancs))
        for c in sorted(x for x in shared if _CLASS.fullmatch(x)):
            yield f"{tag}.{c}{comb}a{base}"
        yield f"{tag}{comb}a{base}"
    yield f"a{base}"


def html_recipe(r: SafeResponse, pattern: str, domain: str, pasted_url: str) -> tuple[dict | None, str]:
    """An html recipe for a listing page whose anchors repeat the pasted job's URL pattern, or
    (None, why not). The items selector must pick out exactly those job links (no nav or filter links)."""
    from jobfinder.ingest.resolve import registrable_domain

    doc = parse_document(page_text(r))
    if doc is None:
        return None, "empty page"
    want = _shape(_rel(pasted_url), pattern, strip_tail=True)  # the listing links the job itself
    base = f"[href*='{pattern}']"
    jobs = []
    for a in select(doc, compile_selector(f"a{base}")):  # what the recipe can select
        href = urljoin(r.url, (a.get("href") or "").strip())
        if (urlsplit(href).scheme in ("http", "https") and registrable_domain(href) == domain
                and _shape(_rel(href), pattern) == want and " ".join(a.itertext()).strip()):
            jobs.append(a)
    if len({a.get("href") for a in jobs}) < MIN_JOB_LINKS:
        return None, f"fewer than {MIN_JOB_LINKS} links like {pattern}"
    target = set(jobs)
    for sel in _selectors(jobs, base):
        if set(select(doc, compile_selector(sel))) == target:
            return {"kind": "html", "list": {"url": r.url}, "items": sel, "pagination": _pages(doc, r.url),
                    "fields": {"external_id": "@href", "title": ":scope", "url": "@href"}}, ""
    return None, f"no selector picks out just the {len(target)} job links"


def _pages(doc, list_url: str) -> dict:
    """Page through the listing when it links to its own page 2 ('?page=2', '?p=2', '?pg=2')."""
    path = urlsplit(list_url).path.rstrip("/")
    for a in select(doc, compile_selector("a[href*='=2']")):
        u = urlsplit(urljoin(list_url, a.get("href") or ""))
        if u.path.rstrip("/") == path:
            for k, v in parse_qsl(u.query):
                if k.lower() in ("page", "p", "pg") and v == "2":
                    return {"kind": "page", "param": k, "start": 1, "step": 1}
    return {"kind": "none"}


def site_name(r: SafeResponse | None) -> str | None:
    doc = parse_document(page_text(r)) if r is not None else None
    if doc is None:
        return None
    meta = select(doc, compile_selector("meta[property='og:site_name']"))
    name = " ".join((meta[0].get("content") or "").split()) if meta else ""
    return name[:200] or None


class ListingRung:
    name = "listing"
    order = 4.0

    async def run(self, ctx: ResolveContext) -> RecipeHit | None:
        return await _Sniff(ctx).run()


class _Sniff:
    def __init__(self, ctx: ResolveContext):
        from jobfinder.ingest.resolve import registrable_domain

        self.ctx = ctx
        self.domain = registrable_domain(ctx.url)
        self.left = MAX_FETCHES
        pasted = ctx.url.split("#")[0]
        self.tried: set[tuple[str, str]] = {("json", pasted), ("page", pasted)}  # (as what, url)
        self.name: str | None = None

    def note(self, msg: str) -> None:
        self.ctx.note(f"listing: {msg}")

    def _same_site(self, url: str) -> bool:
        from jobfinder.ingest.resolve import registrable_domain

        return bool(url) and registrable_domain(url) == self.domain

    async def fetch(self, url: str, accept: tuple[str, ...]) -> SafeResponse | None:
        if not self._same_site(url):
            self.note(f"skipped off-site {url[:150]}")
            return None
        if self.left <= 0:
            self.note("fetch budget used up")
            return None
        self.left -= 1
        self.tried.add(("json" if accept[0] in JSON_TYPES else "page", url))
        try:
            r = await safe_fetch(self.ctx.client, url, accept=accept, max_bytes=PAGE_MAX_BYTES,
                                 robots=self.ctx.robots)
        except FetchError as e:
            self.note(f"{url[:150]}: {e}")
            return None
        if not self._same_site(r.url):
            self.note(f"{url[:150]} redirected off-site")
            return None
        return r

    async def run(self) -> RecipeHit | None:
        page = await self.ctx.page()
        self.name = site_name(page) if page is not None and self._same_site(page.url) else None
        if page is not None and (hit := await self.try_json(page)):
            return hit
        pattern = job_pattern(self.ctx.url)
        if pattern is None:
            self.note("no job URL pattern in the pasted link")
        if hit := await self.try_listings(self.listing_candidates(page, pattern), pattern):
            return hit
        if hit := await self.try_listings(await self.sitemap_candidates(pattern), pattern):
            return hit
        return None

    # (a) JSON endpoints ---------------------------------------------------------------------------
    def json_candidates(self, r: SafeResponse) -> list[str]:
        text = htmllib.unescape(r.text.replace("\\/", "/"))
        scored: dict[str, int] = {}
        for m in _QUOTED_URL.finditer(text):
            raw = m.group(2)
            low = raw.lower()
            if "graphql" in low or "{" in raw or "$" in raw or _STATIC.search(raw.split("#")[0]):
                continue
            api = bool(_APIISH.search(raw))
            if not api and (m.group(1) or not _JOBSISH.search(raw)):  # plain links are pages, not JSON
                continue
            url = safe_http_url(urljoin(r.url, raw)).split("#")[0]
            if not url or ("json", url) in self.tried or not self._same_site(url):
                continue
            scored[url] = 2 * api + bool(_JOBSISH.search(raw))
        return sorted(scored, key=lambda u: -scored[u])[:MAX_JSON_PER_PAGE]

    async def try_json(self, r: SafeResponse) -> RecipeHit | None:
        for url in self.json_candidates(r):
            if ("json", url) in self.tried:
                continue
            resp = await self.fetch(url, JSON_TYPES + ("text/plain",))
            self.ctx.json_tried.add(url)
            if resp is None:
                continue
            try:
                data = resp.json()
            except ValueError:
                self.note(f"{url[:150]}: not JSON")
                continue
            self.ctx.json_samples.append((resp.url, resp.text[:MAX_SAMPLE_CHARS]))  # for the LLM rung
            recipe, why = explain_json_sample(resp.url, "GET", data, pasted_url=self.ctx.url)
            if recipe is None:
                self.note(f"{url[:150]}: {why}")
                continue
            if hit := await self.validate(recipe):
                return hit
        return None

    # (b) listing pages ----------------------------------------------------------------------------
    def listing_candidates(self, page: SafeResponse | None, pattern: str | None) -> list[str]:
        p = urlsplit(self.ctx.url)
        origin = f"{p.scheme}://{p.netloc}"
        out = []
        if pattern and "?" not in pattern and pattern.strip("/"):
            out.append(origin + pattern.rstrip("/"))
        if page is not None and (doc := parse_document(page_text(page))) is not None:
            for a in select(doc, compile_selector("a[href]")):
                href = safe_http_url(urljoin(page.url, (a.get("href") or "").strip())).split("#")[0]
                if href and _LISTING_PATH.search(urlsplit(href).path):
                    out.append(href)
        out += [origin + "/careers", origin + "/jobs"]
        return list(dict.fromkeys(u for u in out if ("page", u) not in self.tried and self._same_site(u)))[:6]

    async def try_listings(self, urls: list[str], pattern: str | None) -> RecipeHit | None:
        for url in urls:
            if ("page", url) in self.tried:
                continue
            if self.left <= 1:  # keep one request to validate what we find
                self.note("fetch budget used up")
                return None
            r = await self.fetch(url, TEXT_TYPES)
            if r is None:
                continue
            if pattern:
                recipe, why = html_recipe(r, pattern, self.domain, self.ctx.url)
                if recipe is not None and (hit := await self.validate(recipe)):
                    return hit
                if recipe is None:
                    self.note(f"{url[:150]}: {why}")
            if hit := await self.try_json(r):  # a JS listing page names its JSON endpoint
                return hit
        return None

    async def sitemap_candidates(self, pattern: str | None) -> list[str]:
        """Listing pages named in the sitemap (robots.txt Sitemap: lines, else /sitemap.xml). Also notes how
        many job URLs the sitemap has with the pasted job's pattern."""
        if self.left <= 2:
            return []
        p = urlsplit(self.ctx.url)
        origin = f"{p.scheme}://{p.netloc}"
        parser = await self.ctx.robots._parser(origin)  # cached by the page fetch
        maps = [u for u in (parser.site_maps() or []) if self._same_site(u)] if parser else []
        r = await self.fetch((maps or [origin + "/sitemap.xml"])[0], TEXT_TYPES)
        if r is None:
            return []
        locs = [htmllib.unescape(x) for x in _LOC.findall(r.text)[:MAX_SITEMAP_LOCS]]
        if pattern:
            jobs = sum(pattern in urlsplit(u).path + "?" + urlsplit(u).query for u in locs)
            self.note(f"sitemap lists {jobs} URLs like {pattern}")
        out = [u for u in locs
               if self._same_site(safe_http_url(u)) and _LISTING_PATH.search(urlsplit(u).path)]
        return list(dict.fromkeys(out))[:3]

    # validation -----------------------------------------------------------------------------------
    async def validate(self, data: dict | Recipe) -> RecipeHit | None:
        try:
            recipe = parse_recipe(data)
        except RecipeError as e:
            self.note(str(e))
            return None
        recipe = recipe.model_copy(update={"provenance": {
            "induced_by": "listing_sniff", "induced_at": datetime.now(UTC).isoformat(),
            "validated_with": self.ctx.url}})
        if self.left <= 0:
            self.note("fetch budget used up before validation")
            return None
        pages = 1 if recipe.pagination.kind == "none" else min(recipe.pagination.max_pages, self.left,
                                                                 MAX_VALIDATE_PAGES)
        self.left -= pages
        probe = recipe.model_copy(update={"pagination": recipe.pagination.model_copy(
            update={"max_pages": pages})})
        if not await self.ctx.robots.allowed(recipe.list.url):
            self.note(f"robots.txt disallows {recipe.list.url[:150]}")
            return None
        try:
            postings = await run_recipe(self.ctx.client, self.domain, self.name or self.domain, probe)
        except (FetchError, RecipeError, ValueError) as e:
            self.note(f"{recipe.kind} {recipe.list.url[:150]}: {e}")
            return None
        ok, why = validate_recipe(postings, self.ctx.url)
        if not ok:
            self.note(f"{recipe.kind} {recipe.list.url[:150]}: {why}")
            return None
        return RecipeHit(recipe=recipe, domain=self.domain, name=self.name, postings=postings)
