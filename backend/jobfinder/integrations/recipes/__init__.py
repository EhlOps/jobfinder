"""Recipes: per-domain instructions for reading a careers site that isn't on a known ATS.

A recipe is *data*, never code: a request to make, how to page, where the items are, and where each field
lives (a small JSONPath subset for json_api, CSS selectors for html). Recipes are stored on
companies.recipe for boards with ats='recipe' and run by ingest_company through run_recipe().

Runners register in RECIPE_RUNNERS by kind: json_api.py and html.py; rendered_html is added by a later
unit (see .plans/2026-10-13-adaptive-job-scraping).
"""
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from jobfinder.models.posting import JobPosting

RecipeKind = Literal["json_api", "html", "rendered_html"]
# Headers a recipe may set. Anything that could carry credentials or redirect routing is refused.
ALLOWED_HEADERS = {"accept", "accept-language", "content-type", "tz", "x-requested-with", "referer"}
MAX_PAGES = 50
MAX_ITEMS = 2000
_PLACEHOLDER = re.compile(r"\{(external_id|title|location)\}")


class RecipeError(ValueError):
    """The recipe is malformed or its output can't be used. Message is safe to log and show admins."""


class RecipeUnavailable(RecipeError):
    """The recipe can't run right now for a reason outside the board (the renderer is off or not answering).
    Ingest skips the board without counting a failure."""


class RecipeRequest(BaseModel):
    method: Literal["GET", "POST"] = "GET"
    url: str = Field(max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | None = None

    @field_validator("url")
    @classmethod
    def _http(cls, v: str) -> str:
        p = urlsplit(v)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            raise ValueError("recipe URL must be an http(s) URL without credentials")
        return v

    @field_validator("headers")
    @classmethod
    def _safe_headers(cls, v: dict[str, str]) -> dict[str, str]:
        bad = [k for k in v if k.lower() not in ALLOWED_HEADERS]
        if bad:
            raise ValueError(f"recipe may not set headers {bad}")
        if any(len(x) > 500 or "\n" in x or "\r" in x for x in v.values()):
            raise ValueError("invalid header value")
        return v


class Pagination(BaseModel):
    """How to fetch more pages. `param` is a query parameter (GET) or a top-level json_body key (POST)."""
    kind: Literal["none", "page", "offset"] = "none"
    param: str | None = Field(default=None, max_length=100)
    start: int = Field(default=0, ge=0)
    step: int = Field(default=1, ge=1, le=1000)  # page: +1 per page; offset: page size
    max_pages: int = Field(default=20, ge=1, le=MAX_PAGES)


class FieldMap(BaseModel):
    """Where each field lives inside one item. For json_api: '$.a.b', '$.a[0].b'. For html: CSS selectors,
    with '@attr' to read an attribute. `url` may instead be a template like 'https://x.com/jobs/{external_id}'."""
    external_id: str = Field(max_length=300)
    title: str = Field(max_length=300)
    url: str = Field(max_length=2048)
    location: str | None = Field(default=None, max_length=300)
    posted_at: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=300)


class Recipe(BaseModel):
    version: Literal[1] = 1
    kind: RecipeKind
    list: RecipeRequest
    pagination: Pagination = Field(default_factory=Pagination)
    items: str = Field(max_length=300)  # path (json) or selector (html) of the repeated job item
    fields: FieldMap
    provenance: dict[str, Any] = Field(default_factory=dict)  # induced_by, induced_at, validated_with


Runner = Callable[[httpx.AsyncClient, Recipe, str, str], Awaitable[list[JobPosting]]]
RECIPE_RUNNERS: dict[str, Runner] = {}


def parse_recipe(data: dict | Recipe | None) -> Recipe:
    if isinstance(data, Recipe):
        return data
    if not data:
        raise RecipeError("board has no recipe")
    try:
        return Recipe.model_validate(data)
    except ValidationError as e:
        raise RecipeError(f"invalid recipe: {e.errors()[0].get('msg', 'bad value')}") from None


async def run_recipe(client: httpx.AsyncClient, slug: str, company_name: str, recipe: dict | Recipe | None
                     ) -> list[JobPosting]:
    """Fetch a recipe board. `slug` (the registrable domain) namespaces external ids, since every recipe
    board shares source='recipe'."""
    r = parse_recipe(recipe)
    runner = RECIPE_RUNNERS.get(r.kind)
    if runner is None:
        raise RecipeError(f"no runner for recipe kind '{r.kind}'")
    return await runner(client, r, slug, company_name)


def fill_template(template: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), template)


def _norm(url: str) -> str:
    p = urlsplit((url or "").strip())
    return f"{(p.hostname or '').lower().removeprefix('www.')}{p.path.rstrip('/')}".lower()


def recipe_finds(postings: list[JobPosting], pasted_url: str) -> bool:
    """True when the pasted job is among the postings: same URL (ignoring scheme, www, query, slash),
    or a posting's id appears as a whole path/query token of the pasted URL."""
    target = _norm(pasted_url)
    tokens = set(re.split(r"[/?&=#._-]+", pasted_url))
    for p in postings:
        if _norm(p.url) == target:
            return True
        eid = p.external_id.split(":", 1)[-1]
        if len(eid) >= 4 and eid in tokens:
            return True
    return False


def validate_recipe(postings: list[JobPosting], pasted_url: str | None) -> tuple[bool, str]:
    """A recipe counts only if it returns jobs and, when we have one, the job the user pasted."""
    if not postings:
        return False, "recipe returned no jobs"
    if pasted_url and not recipe_finds(postings, pasted_url):
        return False, "recipe's results don't include the pasted job"
    return True, f"{len(postings)} jobs"


from jobfinder.integrations.recipes import (
    html as _html,  # noqa: F401  (registers the runner)
)
from jobfinder.integrations.recipes import (
    json_api as _json_api,  # noqa: F401  (registers the runner)
)
from jobfinder.integrations.recipes import (
    rendered as _rendered,  # noqa: F401  (registers the rendered_html runner)
)
