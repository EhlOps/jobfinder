"""Eightfold career sites ({tenant}.eightfold.ai or a custom domain such as apply.careers.microsoft.com).

Slug format: "{host}__{domain}", e.g. "explore.jobs.netflix.net__netflix.com"; `domain` is the tenant key
every API call takes. Tenants expose one of two JSON APIs: PCSX (newer; Microsoft) or the legacy
/api/apply/v2 (Netflix). PCSX answers 403 "PCSX is not enabled for this user." where it is off, so we try it
first and fall back to v2. Both list 10 positions per page with an empty description, so descriptions come
from one detail call per posting (capped). The host comes from the slug, so it is checked to be public and
every request goes through safe_fetch with robots.txt honoured (these sites allow only /api/apply,
/api/pcsx and /careers).
"""
import asyncio
import html
import re
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from jobfinder.core.urls import safe_http_url
from jobfinder.integrations import http_safety
from jobfinder.integrations.robots import PoliteFetcher
from jobfinder.models.posting import (
    JobPosting,
    clip,
    html_to_text,
    infer_seniority,
    infer_workplace,
    parse_salary_range,
)

PCSX_LIST = "https://{host}/api/pcsx/search?domain={domain}&query=&location=&start={start}&sort_by=timestamp"
PCSX_DETAIL = "https://{host}/api/pcsx/position_details?position_id={id}&domain={domain}&hl=en"
V2_LIST = "https://{host}/api/apply/v2/jobs?domain={domain}&start={start}&num={num}"
V2_DETAIL = "https://{host}/api/apply/v2/jobs/{id}?domain={domain}"
JOB_URL = "https://{host}/careers/job/{id}?domain={domain}"
PAGE = 10  # both APIs return 10 positions per page whatever `num` asks for
MAX_PAGES = 300  # 3,000 postings (Microsoft lists ~2,400); a partial list would deactivate the rest
MAX_DETAILS = 200  # per-board cap on description fetches; the rest keep title/location only
DETAIL_CONCURRENCY = 4
MAX_ATTEMPTS = 3  # these sites answer bursts with 429 "Please try again later"
MAX_BYTES = 2_000_000  # the v2 list starts with a branding blob (~10 KB per page in practice)

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(rf"^(?=.{{4,190}}$)(?:{_LABEL}\.)+[a-z]{{2,63}}$")
_ID_RE = re.compile(r"^\d{1,20}$")
_JOB_PATH_RE = re.compile(r"^/careers/job/(\d{1,20})(?:-[^/]*)?/?$")
_SUFFIX = ".eightfold.ai"
_RESERVED = {"www", "app", "api", "static", "docs", "blog", "careers", "help", "support", "login", "sso"}
_api_for: dict[str, str] = {}  # slug -> "pcsx" | "v2", learned on the first fetch
_sleep = asyncio.sleep  # patched in tests


def _host_ok(host: str) -> bool:
    return bool(_HOST_RE.match(host or ""))


def make_slug(host: str, domain: str) -> str | None:
    host, domain = (host or "").lower().rstrip("."), (domain or "").lower().rstrip(".")
    if not (_host_ok(host) and _host_ok(domain)) or "eightfold" in domain:
        return None
    slug = f"{host}__{domain}"
    return slug if len(slug) <= 200 else None  # validate.SLUG_RE's limit


def parse_slug(slug: str) -> tuple[str, str]:
    """(host, domain) from "{host}__{domain}"; ValueError when either part is not a plain hostname."""
    parts = (slug or "").split("__")
    if len(parts) != 2 or make_slug(*parts) != slug:
        raise ValueError(f"invalid eightfold slug '{slug}' (expected host__domain)")
    return parts[0], parts[1]


def board_from_url(host: str, path: str, query: str) -> tuple[str, str] | None:
    """An Eightfold job/careers URL -> ("eightfold", "host__domain"), or None.
    {tenant}.eightfold.ai/careers... is recognised on its own (the domain defaults to {tenant}.com, which
    validation then has to confirm through the API); any other host only with a /careers/job/{id} path and a
    ?domain= parameter, the shape Eightfold's job links take."""
    domain = (parse_qs(query).get("domain") or [""])[0].strip()
    if host.endswith(_SUFFIX):
        tenant = host[: -len(_SUFFIX)]
        if "." in tenant or tenant in _RESERVED or not (path in ("", "/") or path.startswith("/careers")):
            return None
        slug = make_slug(host, domain or f"{tenant}.com")
    elif domain and _JOB_PATH_RE.match(path):
        slug = make_slug(host, domain)
    else:
        return None
    return ("eightfold", slug) if slug else None


_DOMAIN_PARAM_RE = re.compile(r"[?&]domain=([A-Za-z0-9.-]{4,190})")
_DOMAIN_JSON_RE = re.compile(r'"domain"\s*:\s*"([A-Za-z0-9.-]{4,190})"')
# Eightfold's own asset CDN / API paths; a bare "eightfold.ai" link (blog post, privacy policy) is not enough
_MARKER_RE = re.compile(r"static\.vscdn\.net/|eightfold-font|/api/pcsx/|/api/apply/v2/", re.IGNORECASE)


def detect_html(page_url: str, page_html: str) -> tuple[str, str] | None:
    """HTML detector: a page served by Eightfold (its assets, API paths) on any host -> (ats, slug). The tenant
    domain comes from the page URL's ?domain= or else the value the page's own links/config use most."""
    if not page_html or not _MARKER_RE.search(page_html):
        return None
    parts = urlsplit(page_url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not _host_ok(host):
        return None
    domain = (parse_qs(parts.query).get("domain") or [""])[0]
    if not domain:
        text = html.unescape(page_html)
        seen = Counter(d.lower() for d in _DOMAIN_PARAM_RE.findall(text) + _DOMAIN_JSON_RE.findall(text))
        domain = next((d for d, _ in seen.most_common() if "eightfold" not in d and _host_ok(d)), "")
    slug = make_slug(host, domain)
    return ("eightfold", slug) if slug else None


# ── parsing ───────────────────────────────────────────────────────────────
def _epoch(v) -> datetime | None:
    try:
        ts = float(v)
    except (TypeError, ValueError):
        return None
    if not 946684800 <= ts <= 4102444800:  # 2000..2100; anything else is junk (or milliseconds)
        return None
    return datetime.fromtimestamp(ts, UTC)


def _location(it: dict) -> str:
    if isinstance(it.get("location"), str) and it["location"].strip():
        return it["location"].strip()
    locs = it.get("locations")
    return "; ".join(str(x).strip() for x in locs if str(x).strip())[:500] if isinstance(locs, list) else ""


def _first(v) -> str | None:
    if isinstance(v, list):
        v = v[0] if v else None
    return (v.strip() or None) if isinstance(v, str) else None


def _job_url(it: dict, host: str, domain: str, pid: str) -> str:
    for key in ("publicUrl", "canonicalPositionUrl"):
        url = safe_http_url(it.get(key))
        if url:
            return url
    return JOB_URL.format(host=host, id=pid, domain=quote(domain))


def parse_eightfold(items: list[dict], company_name: str, slug: str) -> list[JobPosting]:
    """`items` are PCSX or v2 positions; a "detail" key may hold that position's detail object."""
    host, domain = parse_slug(slug)
    out = []
    for raw in items:
        it = {**raw, **{k: v for k, v in (raw.get("detail") or {}).items() if v not in (None, "", [])}}
        pid = str(raw.get("id") or "").strip()
        title = str(it.get("name") or it.get("posting_name") or "").strip()
        if not title or not _ID_RE.match(pid):
            continue
        location = _location(it)
        desc = html_to_text(str(it.get("jobDescription") or it.get("job_description") or ""))
        sal = parse_salary_range(desc) or (None, None, None)
        declared = it.get("workLocationOption") or it.get("work_location_option")
        if not isinstance(declared, str):
            declared = None
        elif declared.lower().startswith("remote"):  # "remote_local", "remote_global"
            declared = "remote"
        out.append(
            JobPosting(
                source="eightfold",
                external_id=f"{domain}:{pid}",  # position ids are per tenant
                company_name=company_name,
                title=title[:500],
                url=_job_url(it, host, domain, pid),
                location=location,
                workplace_type=infer_workplace(location, title, declared=declared),
                employment_type=_first(it.get("efcustomTextEmploymentType")),
                description_text=clip(desc),
                salary_min=sal[0],
                salary_max=sal[1],
                salary_currency=sal[2],
                seniority=infer_seniority(title),
                posted_at=_epoch(it.get("postedTs") or it.get("t_create") or it.get("t_update")),
            )
        )
    return out


# ── fetching ──────────────────────────────────────────────────────────────
class _Site:
    """One board's requests: SSRF-safe, robots-checked, retried on 429/503 with the cookies the 429 set
    (Eightfold's rate limiter answers a cookieless burst with 429 and lets the same client through after)."""

    def __init__(self, client, host: str, domain: str):
        # discovery hands fetchers a robots-checking wrapper (_PoliteClient) that only has get/post;
        # safe_fetch needs the httpx client underneath, and we reuse its PoliteFetcher
        fetcher = getattr(client, "_fetcher", None)
        if isinstance(fetcher, PoliteFetcher):
            self.client, self.robots = fetcher.client, fetcher
        else:
            self.client, self.robots = client, PoliteFetcher(client)
        self.host, self.domain = host, domain
        self.cookies: dict[str, str] = {}

    def _keep_cookies(self, headers: httpx.Headers | None) -> None:
        for v in headers.get_list("set-cookie") if headers else []:
            name, _, value = v.split(";", 1)[0].partition("=")
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name.strip()) and len(value) <= 512:
                self.cookies[name.strip()] = value.strip()

    async def get_json(self, url: str):
        """GET JSON; an HTTP error status surfaces as httpx.HTTPStatusError, like the other ATS adapters."""
        for attempt in range(MAX_ATTEMPTS):
            headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())} if self.cookies else None
            r = await http_safety.safe_fetch(self.client, url, headers=headers, accept=http_safety.JSON_TYPES,
                                             robots=self.robots, max_bytes=MAX_BYTES, raise_for_status=False)
            self._keep_cookies(r.headers)
            if r.status in (429, 503) and attempt < MAX_ATTEMPTS - 1:
                await _sleep(2.0 ** attempt)
                continue
            if r.status >= 400:
                req = httpx.Request("GET", url)
                raise httpx.HTTPStatusError(f"HTTP {r.status}", request=req,
                                            response=httpx.Response(r.status, request=req, content=r.body))
            return r.json()
        raise AssertionError("unreachable")

    async def list(self, api: str, pages: int) -> list[dict]:
        listed: list[dict] = []
        total = 0
        for page in range(pages):
            fmt = {"host": self.host, "domain": self.domain, "start": page * PAGE, "num": PAGE}
            if api == "pcsx":
                data = ((await self.get_json(PCSX_LIST.format(**fmt))) or {}).get("data") or {}
            else:
                data = (await self.get_json(V2_LIST.format(**fmt))) or {}
            positions = [p for p in data.get("positions") or [] if isinstance(p, dict)]
            listed.extend(positions)
            count = data.get("count")
            total = total or (count if isinstance(count, int) and count > 0 else 0)
            if not positions or (total and len(listed) >= total):
                break
        return listed

    async def detail(self, api: str, pid: str) -> dict:
        if api == "pcsx":
            d = ((await self.get_json(PCSX_DETAIL.format(host=self.host, id=pid, domain=self.domain))) or {}).get("data")
        else:
            d = await self.get_json(V2_DETAIL.format(host=self.host, id=pid, domain=self.domain))
        return d if isinstance(d, dict) else {}


def _pcsx_disabled(e: httpx.HTTPStatusError) -> bool:
    """403 "PCSX is not enabled for this user." (or a 404 naming PCSX): this tenant only has the v2 API."""
    code = e.response.status_code
    return code in (403, 404) and b"pcsx" in e.response.content.lower()


async def fetch_eightfold(
    client: httpx.AsyncClient, slug: str, company_name: str, list_only: bool = False
) -> list[JobPosting]:
    """list_only fetches just the first listing page and skips the per-posting detail calls (cheap probe)."""
    host, domain = parse_slug(slug)
    await http_safety.assert_public_host(host)
    site = _Site(client, host, domain)
    pages = 1 if list_only else MAX_PAGES
    api = _api_for.pop(slug, "pcsx")  # re-learned below, so a tenant that switches APIs recovers
    try:
        raw = await site.list(api, pages)
    except httpx.HTTPStatusError as e:
        if api != "pcsx" or not _pcsx_disabled(e):
            raise
        api = "v2"
        raw = await site.list(api, pages)
    _api_for[slug] = api
    by_id: dict[str, dict] = {}
    for p in raw:  # paging by offset over a changing list can repeat a posting
        pid = str(p.get("id") or "")
        if _ID_RE.match(pid):
            by_id.setdefault(pid, p)
    listed = list(by_id.values())

    if list_only:
        return await asyncio.to_thread(parse_eightfold, listed, company_name, slug)

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    failed = 0

    async def detail(item: dict) -> dict:
        nonlocal failed
        async with sem:
            try:
                return {**item, "detail": await site.detail(api, str(item["id"]))}
            except Exception:  # keep the listed posting; the upsert preserves any stored description
                failed += 1
                return item

    head, tail = listed[:MAX_DETAILS], listed[MAX_DETAILS:]
    details = list(await asyncio.gather(*(detail(i) for i in head)))
    if len(head) >= 3 and failed == len(head):
        raise RuntimeError(f"all {failed} posting detail requests failed")
    return await asyncio.to_thread(parse_eightfold, details + tail, company_name, slug)
