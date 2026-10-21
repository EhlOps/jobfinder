"""rendered_html recipes: the jobs exist only in the DOM a browser builds, so every fetch goes through the
renderer container (a render_list task) and its items are mapped here like json_api items. Rare and capped
(settings.renderer_max_rendered_boards); refused outright while the renderer is switched off."""
from urllib.parse import urljoin, urlsplit

import httpx
import sqlalchemy as sa

from jobfinder import db as app_db  # the module: tests swap db.SessionLocal for the test database
from jobfinder.core.config import settings
from jobfinder.ingest.base import JobPosting
from jobfinder.integrations.recipes import (
    MAX_ITEMS,
    RECIPE_RUNNERS,
    Recipe,
    RecipeError,
    RecipeUnavailable,
    fill_template,
)
from jobfinder.integrations.recipes.json_api import make_posting, parse_date
from jobfinder.integrations.render import LIST, RenderError, RenderUnavailable, request_render
from jobfinder.models import Company

LIST_WAIT_S = 150.0  # ingest runs boards concurrently and the renderer takes two at a time, 30 s each
_FIELDS = ("external_id", "title", "url", "location", "posted_at", "description")


def item_id(value: str, base_url: str) -> str:
    """A stable id from an item's id field. Links (the usual case, '@href') become their path and query."""
    value = (value or "").strip()
    if "/" in value or value.startswith(("http:", "https:")):
        p = urlsplit(urljoin(base_url, value))
        value = p.path.rstrip("/") + (f"?{p.query}" if p.query else "")
    return value[:190]


async def run_rendered(client: httpx.AsyncClient, recipe: Recipe, slug: str, company_name: str, *,
                       wait_s: float = LIST_WAIT_S) -> list[JobPosting]:
    if not settings.renderer_enabled:
        raise RecipeUnavailable("this board needs the renderer, which is switched off (RENDERER_ENABLED)")
    try:
        result = await request_render(LIST, {"recipe": recipe.model_dump(mode="json")}, timeout_s=wait_s)
    except RenderUnavailable as e:
        raise RecipeUnavailable(str(e)) from None
    except RenderError as e:
        raise RecipeError(f"render failed: {e}") from None
    base = str(result.get("final_url") or recipe.list.url)
    out, seen = [], set()
    for raw in (result.get("items") or [])[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        v = {k: (raw.get(k) or "").strip() if isinstance(raw.get(k), str) else "" for k in _FIELDS}
        # a link's text often runs on into its location/date lines: the first line is the title
        first = next((ln for ln in v["title"].splitlines() if ln.strip()), "")
        eid, title = item_id(v["external_id"], base), " ".join(first.split())
        if not eid or not title:
            continue
        if "{" in recipe.fields.url:  # a URL template is filled here, never sent to the renderer
            url = fill_template(recipe.fields.url, {"external_id": eid, "title": title, "location": v["location"]})
        else:
            url = urljoin(base, v["url"]) if v["url"] else ""
        p = make_posting(slug, company_name, eid=eid, title=title, url=url, location=v["location"],
                         description=v["description"], posted_at=parse_date(v["posted_at"] or None))
        if p.external_id not in seen:
            seen.add(p.external_id)
            out.append(p)
    return out


async def count_rendered_boards(session_factory=None) -> int:
    """Recipe boards that need the browser on every fetch (for the cap)."""
    if session_factory is None:
        session_factory = app_db.SessionLocal
    async with session_factory() as db:
        return await db.scalar(
            sa.select(sa.func.count()).select_from(Company)
            .where(Company.ats == "recipe", Company.recipe["kind"].as_string() == "rendered_html")
        ) or 0


RECIPE_RUNNERS["rendered_html"] = run_rendered
