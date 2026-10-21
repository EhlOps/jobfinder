"""Rung 4.5: render the pasted page in the sandboxed browser (opt-in, settings.renderer_enabled).

The renderer records the JSON (XHR/fetch) responses the page makes. Most "JS-only" careers sites load their jobs
from such an endpoint, so the first that becomes a working json_api recipe (one that finds the pasted job)
wins, and from then on the site is fetched with plain httpx. Only if none does, and the rendered DOM repeats
links shaped like the pasted job's, is a rendered_html recipe made (validated through the renderer too); those
need the browser on every fetch, so they are capped.
"""
import json
import logging
import re
import time
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

from lxml import etree
from lxml import html as lxml_html
from pydantic import ValidationError

from jobfinder.core.config import settings
from jobfinder.core.urls import registrable_domain
from jobfinder.ingest.http_safety import JSON_TYPES, FetchError, safe_fetch
from jobfinder.integrations.recipes import Recipe, RecipeError, run_recipe, validate_recipe
from jobfinder.integrations.recipes.rendered import count_rendered_boards, run_rendered
from jobfinder.integrations.recipes.sniff import explain_json_sample
from jobfinder.integrations.render import CAPTURE, RenderError, RenderUnavailable, request_render
from jobfinder.integrations.render.guard import check_selector
from jobfinder.services.resolver.ladder import RecipeHit, ResolveContext
from jobfinder.services.resolver.names import name_from_domain
from jobfinder.services.resolver.rungs.listing import MAX_SAMPLE_CHARS, job_pattern

log = logging.getLogger("jobfinder.resolve")

CAPTURE_WAIT_S = 60.0
RUNG_BUDGET_S = 180.0  # the whole rung: two captures, recipe runs and a render_list at most
MIN_STEP_S = 10.0
MAX_CANDIDATES = 4  # json endpoints turned into recipes and run, per captured page
SAMPLE_MAX_BYTES = 3_000_000
MIN_JOB_LINKS = 2
MAX_LLM_SAMPLES = 6
_SIDE_LIST = re.compile(r"similar|related|recommend|suggest|insight|recent", re.IGNORECASE)


def listing_url(url: str) -> str | None:
    """A guess at the listing page behind a job URL: its shared prefix ('/careers/positions/5502' ->
    '/careers/positions'). The job's own page often shows only itself and a few 'similar jobs'."""
    pattern = job_pattern(url)
    if pattern is None:
        return None
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{pattern.split('?')[0].rstrip('/')}"


def rendered_html_recipe(html: str, page_url: str, pasted_url: str) -> tuple[dict | None, str]:
    """A rendered_html recipe when the rendered DOM repeats links shaped like the pasted job's. Links are
    counted exactly as the recipe's selector will match them (on the raw href)."""
    pattern = job_pattern(pasted_url)
    if pattern is None:
        return None, "the pasted URL has no listing-like path"
    try:
        doc = lxml_html.fromstring(html or "<html></html>")
    except (ValueError, etree.ParserError):
        return None, "unreadable rendered page"
    domain = registrable_domain(pasted_url)
    links = set()
    for a in doc.iter("a"):
        raw = (a.get("href") or "").strip()
        href = urljoin(page_url, raw)
        p = urlsplit(href)
        if (pattern in raw and not raw.endswith(pattern) and p.scheme in ("http", "https")
                and registrable_domain(href) == domain and a.text_content().strip()):
            links.add(p.path)
    if len(links) < MIN_JOB_LINKS:
        return None, f"fewer than {MIN_JOB_LINKS} rendered links matching {pattern}"
    items = f"a[href*='{pattern}']:not([href$='{pattern}'])"
    try:
        check_selector(items)
    except ValueError:
        return None, f"job links {pattern!r} can't be selected safely"
    return {"kind": "rendered_html", "list": {"url": page_url}, "items": items,
            "fields": {"external_id": "@href", "title": ":scope", "url": "@href"}}, ""


class RenderedRung:
    name = "rendered"
    order = 4.5

    async def run(self, ctx: ResolveContext) -> RecipeHit | None:
        if not settings.renderer_enabled:
            return None
        deadline = time.monotonic() + RUNG_BUDGET_S
        domain = registrable_domain(ctx.url)
        pages = [ctx.url]
        guess = listing_url(ctx.url)
        if guess and guess.rstrip("/") != ctx.url.rstrip("/"):
            pages.append(guess)
        captures = []
        for page_url in pages:
            remaining = deadline - time.monotonic()
            if remaining < MIN_STEP_S:
                break
            try:
                capture = await request_render(CAPTURE, {"url": page_url},
                                               timeout_s=min(CAPTURE_WAIT_S, remaining))
            except RenderUnavailable as e:
                ctx.note(f"rendered: {e}")
                return None
            except RenderError as e:
                ctx.note(f"rendered: {page_url[:100]}: {str(e)[:150]}")
                continue
            captures.append(capture)
            hit = await self._from_json(ctx, capture, domain, deadline)
            if hit:
                return hit
        for capture in reversed(captures):  # the listing page's DOM first
            if deadline - time.monotonic() < MIN_STEP_S:
                break
            hit = await self._rendered_dom(ctx, capture, domain, deadline)
            if hit:
                return hit
        return None

    async def _from_json(self, ctx: ResolveContext, capture: dict, domain: str, deadline: float
                         ) -> RecipeHit | None:
        tried = 0
        responses = [r for r in capture.get("json_responses") or [] if isinstance(r, dict)]
        for r in responses:  # what the page itself called: hints for the LLM rung if nothing here works
            url = str(r.get("url") or "")
            if url in ctx.json_tried:
                continue
            ctx.json_tried.add(url)
            if (registrable_domain(url) == domain and len(ctx.json_samples) < MAX_LLM_SAMPLES
                    and r.get("method") == "GET" and r.get("body_preview")):
                ctx.json_samples.append((url, str(r["body_preview"])[:MAX_SAMPLE_CHARS]))
        for resp in sorted(responses, key=_candidate_order):
            if tried >= MAX_CANDIDATES or deadline - time.monotonic() < MIN_STEP_S:
                break
            body = _json_body(resp)
            sample = await self._sample(ctx, resp, body)
            if sample is None:
                continue
            recipe, _why = explain_json_sample(str(resp.get("url")), str(resp.get("method") or "GET"),
                                               sample, body, pasted_url=ctx.url)
            if recipe is None:
                continue
            tried += 1
            hit = await self._validate(ctx, recipe, domain, deadline)
            if hit:
                return hit
        return None

    async def _sample(self, ctx: ResolveContext, resp: dict, body: dict | None):
        """The response's JSON: from the capture, or fetched again (SSRF-checked) when the preview was cut."""
        if not resp.get("truncated"):
            try:
                return json.loads(resp.get("body_preview") or "")
            except ValueError:
                return None
        method = str(resp.get("method") or "GET").upper()
        if method not in ("GET", "POST") or (method == "POST" and body is None):
            return None
        try:
            r = await safe_fetch(ctx.client, str(resp.get("url")), method=method, json=body,
                                 headers={"accept": "application/json"}, accept=JSON_TYPES + ("text/plain",),
                                 max_bytes=SAMPLE_MAX_BYTES)
            return r.json()
        except (FetchError, ValueError):
            return None

    async def _validate(self, ctx: ResolveContext, data: dict | Recipe, domain: str, deadline: float
                        ) -> RecipeHit | None:
        provenance = {"induced_by": "rendered", "induced_at": datetime.now(UTC).isoformat(),
                      "validated_with": ctx.url}
        data = data.model_dump(mode="json") if isinstance(data, Recipe) else dict(data)
        data["provenance"] = provenance
        try:
            recipe = Recipe.model_validate(data)
            name = name_from_domain(domain)
            if recipe.kind == "rendered_html":  # bounded by what's left of the rung's budget
                postings = await run_rendered(ctx.client, recipe, domain, name,
                                              wait_s=max(deadline - time.monotonic(), MIN_STEP_S))
            else:
                postings = await run_recipe(ctx.client, domain, name, recipe)
        except (ValidationError, RecipeError, FetchError) as e:
            ctx.note(f"rendered: {data['kind']} {data['list']['url'][:100]}: {str(e)[:120]}")
            return None
        ok, why = validate_recipe(postings, ctx.url)
        if not ok:
            ctx.note(f"rendered: {data['kind']} {data['list']['url'][:100]}: {why}")
            return None
        return RecipeHit(recipe=recipe, domain=domain, postings=postings)

    async def _rendered_dom(self, ctx: ResolveContext, capture: dict, domain: str, deadline: float
                            ) -> RecipeHit | None:
        data, why = rendered_html_recipe(str(capture.get("html") or ""), str(capture.get("final_url") or ctx.url),
                                         ctx.url)
        if data is None:
            ctx.note(f"rendered: {why}")
            return None
        cap = settings.renderer_max_rendered_boards
        if await count_rendered_boards() >= cap:
            ctx.note(f"rendered: the cap of {cap} browser-rendered boards is reached")
            return None
        return await self._validate(ctx, data, domain, deadline)


def _candidate_order(resp: dict) -> tuple[bool, int]:
    """Search/list endpoints first: a job page's 'similar jobs' call also returns a list (which may even
    contain the pasted job) but never the whole board. Then the biggest responses."""
    return bool(_SIDE_LIST.search(str(resp.get("url") or ""))), -int(resp.get("size") or 0)


def _json_body(resp: dict) -> dict | None:
    """The captured request body when it is a JSON object (POST endpoints need it replayed)."""
    raw = resp.get("request_body")
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None
