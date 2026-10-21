"""json_api recipes: call a JSON endpoint (the one the careers page itself uses), page through it, and map
each item with a small JSONPath subset: $ . key [n] [*] (no filters, no scripts)."""
import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from jobfinder.core.urls import safe_http_url
from jobfinder.integrations.http_safety import JSON_TYPES, SafeResponse, safe_fetch
from jobfinder.integrations.recipes import (
    MAX_ITEMS,
    RECIPE_RUNNERS,
    Recipe,
    RecipeError,
    fill_template,
)
from jobfinder.models.posting import (
    JobPosting,
    clip,
    html_to_text,
    infer_seniority,
    infer_workplace,
    parse_salary_range,
)

_TOKEN = re.compile(r"\.([A-Za-z0-9_\-$@]+)|\[(\d+|\*)\]|\[\"([^\"]+)\"\]|\['([^']+)'\]")


def compile_path(path: str) -> list[str | int]:
    """'$.data.jobs[*].id' -> ['data', 'jobs', '*', 'id']. Raises RecipeError on anything else."""
    path = (path or "").strip()
    if not path.startswith("$"):
        raise RecipeError(f"path must start with $: {path!r}")
    out: list[str | int] = []
    pos = 1
    while pos < len(path):
        m = _TOKEN.match(path, pos)
        if not m:
            raise RecipeError(f"unsupported path syntax: {path!r}")
        key, idx, dq, sq = m.groups()
        if key is not None:
            out.append(key)
        elif idx is not None:
            out.append("*" if idx == "*" else int(idx))
        else:
            out.append(dq if dq is not None else sq)
        pos = m.end()
    return out


def get_all(data, path: str) -> list:
    nodes = [data]
    for step in compile_path(path):
        nxt = []
        for n in nodes:
            if step == "*":
                if isinstance(n, list):
                    nxt.extend(n)
                elif isinstance(n, dict):
                    nxt.extend(n.values())
            elif isinstance(step, int):
                if isinstance(n, list) and -len(n) <= step < len(n):
                    nxt.append(n[step])
            elif isinstance(n, dict) and step in n:
                nxt.append(n[step])
        nodes = nxt
    return nodes


def get_one(data, path: str | None):
    if not path:
        return None
    found = get_all(data, path)
    return found[0] if found else None


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(_text(x) for x in v if x is not None)
    if isinstance(v, dict):
        return str(v.get("name") or v.get("label") or v.get("text") or "")
    return str(v).strip()


def parse_date(v) -> datetime | None:
    """ISO strings, YYYY-MM-DD, or epoch seconds/milliseconds."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, int | float) or (isinstance(v, str) and v.isdigit()):
            n = float(v)
            return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=UTC)
        d = datetime.fromisoformat(str(v))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def map_item(item, recipe: Recipe, slug: str, company_name: str, base_url: str) -> JobPosting | None:
    f = recipe.fields
    eid = _text(get_one(item, f.external_id))
    title = _text(get_one(item, f.title))
    if not eid or not title:
        return None
    location = _text(get_one(item, f.location))
    values = {"external_id": eid, "title": title, "location": location}
    url = _text(get_one(item, f.url)) if f.url.startswith("$") else fill_template(f.url, values)
    if url.startswith("/"):
        p = urlsplit(base_url)
        url = f"{p.scheme}://{p.netloc}{url}"
    return make_posting(slug, company_name, eid=eid, title=title, url=url, location=location,
                        description=_text(get_one(item, f.description)),
                        posted_at=parse_date(get_one(item, f.posted_at)))


def make_posting(slug: str, company_name: str, *, eid: str, title: str, url: str, location: str,
                 description: str, posted_at: datetime | None) -> JobPosting:
    """A recipe posting from extracted raw values (shared by every recipe runner)."""
    desc = clip(html_to_text(description)) if "<" in description else clip(description)
    lo, hi, cur = parse_salary_range(desc) or (None, None, None)
    return JobPosting(
        source="recipe", external_id=f"{slug}:{eid}"[:200], company_name=company_name, title=title[:500],
        url=safe_http_url(url), location=location[:500], workplace_type=infer_workplace(location, title),
        description_text=desc, salary_min=lo, salary_max=hi, salary_currency=cur,
        seniority=infer_seniority(title), posted_at=posted_at,
    )


def page_request(recipe: Recipe, page: int) -> tuple[str, dict | None]:
    """The (url, json body) for page `page` (0-based): the pagination value goes in the query or body."""
    req, pg = recipe.list, recipe.pagination
    url, body = req.url, dict(req.json_body) if req.json_body is not None else None
    if pg.kind == "none" or not pg.param:
        return url, body
    value = pg.start + page * pg.step
    if req.method == "POST" and body is not None:
        body[pg.param] = value
        return url, body
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != pg.param]
    q.append((pg.param, str(value)))
    return urlunsplit(parts._replace(query=urlencode(q))), body


def _map_page(r: SafeResponse, recipe: Recipe, slug: str, company_name: str) -> list[JobPosting]:
    try:
        data = r.json()
    except ValueError:
        raise RecipeError("endpoint did not return JSON") from None
    mapped = (map_item(item, recipe, slug, company_name, r.url) for item in get_all(data, recipe.items))
    return [p for p in mapped if p]


async def run_json_api(client: httpx.AsyncClient, recipe: Recipe, slug: str, company_name: str
                       ) -> list[JobPosting]:
    pages = 1 if recipe.pagination.kind == "none" else recipe.pagination.max_pages
    out: list[JobPosting] = []
    seen: set[str] = set()
    for page in range(pages):
        url, body = page_request(recipe, page)
        r = await safe_fetch(client, url, method=recipe.list.method, headers=recipe.list.headers or None,
                             json=body, accept=JSON_TYPES + ("text/plain",))
        # JSON decode + mapping is CPU-bound on big pages: keep it off the event loop
        added = 0
        for p in await asyncio.to_thread(_map_page, r, recipe, slug, company_name):
            if p.external_id not in seen:
                seen.add(p.external_id)
                out.append(p)
                added += 1
        if not added or len(out) >= MAX_ITEMS:  # empty or repeating page: we've reached the end
            break
    return out[:MAX_ITEMS]


RECIPE_RUNNERS["json_api"] = run_json_api
