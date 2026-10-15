"""Rung 5: ask Claude for a recipe. The last rung, for sites no deterministic rung could read.

Claude gets the pasted page (scripts and styles stripped) and up to three JSON responses from same-site
endpoints the page refers to, all wrapped as untrusted data, and has no tools: it can only return a recipe
(schema-validated JSON). The recipe is then treated like any other: converted to a safe Recipe, every URL in
it must be on the pasted URL's registrable domain, and it must return jobs including the pasted one. One
retry feeds the failure back; after that, or on any AI error, the rung passes. The recipe, not the model,
reads the site from then on.

Also here: reinduce_company(), the self-heal for a recipe board that degraded (scheduling handler
reinduce_recipe), which re-runs the same induction with the old recipe and its failure as context.
"""
import html as htmllib
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ai.claude_code import AIError
from jobfinder.ai.tasks import AITasks
from jobfinder.core.config import settings
from jobfinder.ingest.base import JobPosting, safe_http_url
from jobfinder.ingest.http_safety import JSON_TYPES, FetchError, safe_fetch
from jobfinder.ingest.recipes import Recipe, RecipeError, run_recipe, validate_recipe
from jobfinder.ingest.resolve.ladder import RecipeHit, ResolveContext
from jobfinder.ingest.resolve.rungs.listing import _APIISH, _QUOTED_URL, _STATIC

log = logging.getLogger("jobfinder.resolve.llm")

ATTEMPTS = 2  # the first proposal, then one retry with the failure fed back
MAX_SAMPLE_FETCHES = 3
SAMPLE_MAX_BYTES = 1_000_000
PAGE_SCAN_CHARS = 2_000_000
REINDUCE_MIN_YIELD = 0.5  # a re-induced recipe must return at least half the old baseline (see _reinduce_need)

Check = Callable[[list[JobPosting]], tuple[bool, str]]


def _domain(url: str) -> str:
    from jobfinder.ingest.resolve import registrable_domain  # resolve/__init__ imports the rungs

    return registrable_domain(url)


def _company_name(domain: str) -> str:
    from jobfinder.ingest.resolve import name_from_domain

    return name_from_domain(domain)


def endpoint_candidates(page_html: str, base_url: str, domain: str, limit: int = MAX_SAMPLE_FETCHES,
                        skip: set[str] | frozenset = frozenset()) -> list[str]:
    """Same-site API-looking URLs the page mentions (in markup or inline scripts), in page order. Same
    filters as the listing sniff (rungs/listing.py): entities decoded, no templates, no static assets."""
    out: list[str] = []
    text = htmllib.unescape(page_html[:PAGE_SCAN_CHARS].replace("\\/", "/"))
    here = base_url.split("#")[0]
    for m in _QUOTED_URL.finditer(text):
        raw = m.group(2)
        if "{" in raw or "$" in raw or not _APIISH.search(raw) or _STATIC.search(raw.split("#")[0]):
            continue
        url = safe_http_url(urljoin(base_url, raw)).split("#")[0]
        if url and url not in out and url not in skip and url != here and _domain(url) == domain:
            out.append(url)
            if len(out) >= limit:
                break
    return out


async def json_samples(client: httpx.AsyncClient, urls: list[str], robots=None) -> list[tuple[str, str]]:
    """(url, body) for each URL that answers with JSON. Failures are skipped: samples are only hints."""
    out = []
    for url in urls[:MAX_SAMPLE_FETCHES]:
        try:
            r = await safe_fetch(client, url, accept=JSON_TYPES, max_bytes=SAMPLE_MAX_BYTES, robots=robots)
        except (FetchError, httpx.HTTPError):
            continue
        out.append((r.url, r.text))
    return out


async def induce(
    ai: AITasks, client: httpx.AsyncClient, *, page_url: str, page_html: str, samples: list[tuple[str, str]],
    pasted_url: str, domain: str, check: Check, provenance: dict, previous: dict | None = None,
    failure: str | None = None, note: Callable[[str], None] = lambda _: None,
) -> tuple[Recipe | None, list[JobPosting], str]:
    """Ask for a recipe, convert it safely, run it and `check` the results; one retry with the reason.
    Returns (recipe, postings, "") on success, else (None, [], why). AIError is re-raised: a failing model
    says nothing about the site, so the caller passes (rung) or tries again later (re-induction)."""
    name = _company_name(domain)
    why = ""
    for attempt in range(ATTEMPTS):
        proposed = await ai.induce_recipe(page_url, page_html, samples, pasted_url,
                                          previous=previous, failure=failure)
        try:
            recipe = proposed.to_recipe(domain, provenance={
                **provenance, "induced_at": datetime.now(UTC).isoformat(),
                "confidence": max(0.0, min(1.0, float(proposed.confidence))),
            })
            postings = await run_recipe(client, domain, name, recipe)
            ok, why = check(postings)
        except (RecipeError, FetchError, httpx.HTTPError) as e:
            ok, why = False, str(e) or type(e).__name__
        except Exception as e:  # a model-written recipe must never crash a resolve or a heal
            log.warning("induced recipe for %s crashed: %r", domain, e)
            ok, why = False, f"recipe failed ({type(e).__name__})"
        if ok:
            return recipe, postings, ""
        note(f"attempt {attempt + 1}: {why}")
        previous, failure = proposed.model_dump(exclude={"confidence", "notes"}), why
    return None, [], why


class LlmRung:
    name = "llm"
    order = 5.0

    async def run(self, ctx: ResolveContext) -> RecipeHit | None:
        if not ctx.allow_llm or not settings.resolver_llm_enabled:
            return None
        if ctx.ai is None:
            ctx.note("llm: no AI available")
            return None
        page = await ctx.page()
        if page is None:
            return None
        domain = _domain(ctx.url)
        samples = list(ctx.json_samples[:MAX_SAMPLE_FETCHES])  # what earlier rungs already fetched
        if len(samples) < MAX_SAMPLE_FETCHES:
            urls = endpoint_candidates(page.text, page.url, domain, MAX_SAMPLE_FETCHES - len(samples),
                                       skip={u for u, _ in samples} | ctx.json_tried)
            samples += await json_samples(ctx.client, urls, ctx.robots)
        try:
            recipe, postings, why = await induce(
                ctx.ai, ctx.client, page_url=page.url, page_html=page.text, samples=samples,
                pasted_url=ctx.url, domain=domain, check=lambda ps: validate_recipe(ps, ctx.url),
                provenance={"induced_by": "llm", "model": settings.recipe_model, "validated_with": ctx.url},
                note=lambda m: ctx.note(f"llm: {m}"),
            )
        except AIError as e:
            ctx.note(f"llm: AI unavailable ({e.kind})")
            return None
        if recipe is None:
            ctx.note(f"llm: no working recipe ({why})")
            return None
        return RecipeHit(recipe=recipe, domain=domain, postings=postings)


def _reinduce_need(h: dict) -> int:
    """Jobs a re-induced recipe must return: half the baseline or, when the old recipe still ran cleanly
    (a site that really shrank also reads as degraded), as many as it found, whichever is lower; at least 1."""
    need = math.ceil(REINDUCE_MIN_YIELD * int(h.get("baseline_yield") or 0))
    last = int(h.get("last_yield") or 0)
    if last > 0 and not h.get("last_error"):
        need = min(need, last)
    return max(1, need)


async def reinduce_company(db: AsyncSession, ai: AITasks, company_id: int,
                           client: httpx.AsyncClient | None = None) -> dict:
    """Self-heal one degraded recipe board: re-induce from the page its recipe was validated with (else the
    recipe's list page, else the site root), with the old recipe and its last error as context. The new
    recipe must return _reinduce_need() jobs (the job it was validated with may be long gone).

    Only a verdict on the site is recorded with record_reinduce (a failure disables the board). When the AI
    fails or no page can be fetched, nothing is recorded and "retry": True asks the caller to try later."""
    from jobfinder.ingest.recipes import health
    from jobfinder.ingest.service import deactivate_missing, upsert_postings
    from jobfinder.models import Company

    company = await db.get(Company, company_id)
    if company is None or company.ats != "recipe":
        return {"skipped": "not a recipe board"}
    if not settings.resolver_llm_enabled:
        return {"skipped": "llm disabled"}
    if not health.needs_reinduce(company):
        return {"skipped": "not due", "state": company.recipe_state}
    old = dict(company.recipe or {})
    h = dict(company.recipe_health or {})
    need = _reinduce_need(h)
    prov = dict(old.get("provenance") or {})
    old_list = (old.get("list") or {}).get("url") or ""
    pages = [u for u in (prov.get("validated_with"), old_list if old.get("kind") != "json_api" else None,
                         f"https://{company.slug}/") if u and _domain(u) == company.slug]
    failure = h.get("last_error") or f"yield fell to {h.get('last_yield', 0)} (baseline {h.get('baseline_yield')})"

    own = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=True)
    try:
        page, ctx = None, None
        for url in dict.fromkeys(pages):
            ctx = ResolveContext(url, client, allow_llm=True, ai=ai, robots=ctx.robots if ctx else None)
            if (page := await ctx.page()) is not None:
                break
        samples = []
        if old.get("kind") == "json_api" and old_list and _domain(old_list) == company.slug:
            samples = await json_samples(client, [old_list], ctx.robots)  # what the old endpoint says now
        if page is not None:
            urls = endpoint_candidates(page.text, page.url, company.slug, MAX_SAMPLE_FETCHES - len(samples),
                                       skip={old_list})
            samples += await json_samples(client, urls, ctx.robots)
        if page is None and not samples:  # the site is unreachable right now: that's no verdict on the recipe
            return {"skipped": f"page unavailable ({ctx.page_error})", "retry": True}

        def check(ps: list[JobPosting]) -> tuple[bool, str]:
            if len(ps) < need:
                return False, f"recipe returned {len(ps)} jobs; expected at least {need}"
            return True, f"{len(ps)} jobs"

        validated_with = prov.get("validated_with") or ctx.url
        try:
            recipe, postings, why = await induce(
                ai, client, page_url=page.url if page else ctx.url, page_html=page.text if page else "",
                samples=samples, pasted_url=validated_with, domain=company.slug, check=check, previous=old,
                failure=failure,
                provenance={"induced_by": "llm", "model": settings.recipe_model,
                            "validated_with": validated_with, "reinduced_after": failure[:300]},
            )
        except AIError as e:  # not the site's fault: leave the board degraded and try again later
            return {"skipped": f"AI unavailable ({e.kind})", "retry": True}
        state = health.record_reinduce(company, recipe is not None,
                                       recipe.model_dump(mode="json") if recipe else None)
        if recipe is None:  # disabled now, so never polled again: don't leave its jobs up with dead links
            await deactivate_missing(db, "recipe", company.id, [])  # commits the health change too
            return {"ok": False, "state": state, "reason": why[:300]}
        # the check already fetched the whole board: store that instead of fetching it all again
        await upsert_postings(db, postings, company.id)
        await deactivate_missing(db, "recipe", company.id, [p.external_id for p in postings])
        state = health.record_run(company, len(postings))
        company.last_fetched_at = company.last_success_at = datetime.now(UTC)
        await db.commit()
        return {"ok": True, "state": state, "fetched": len(postings)}
    finally:
        if own:
            await client.aclose()
