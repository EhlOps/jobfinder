"""Validation of a recipe the LLM induced (resolver rung 5): turn claude.schemas.InducedRecipe into a Recipe.

The model's output is deliberately permissive (claude/schemas.py), so a near miss can be explained to it on the
retry. This is where it becomes a real Recipe: every URL must be on the board's own domain, and paths and
selectors are compiled before anything is fetched.
"""
import json
import re
from typing import TYPE_CHECKING

from pydantic import ValidationError

from jobfinder.core.urls import registrable_domain
from jobfinder.integrations.recipes import Recipe, RecipeError, fill_template
from jobfinder.integrations.recipes.html import _is_template, compile_recipe
from jobfinder.integrations.recipes.json_api import compile_path

if TYPE_CHECKING:  # claude.schemas imports this module for InducedRecipe.to_recipe
    from jobfinder.integrations.claude.schemas import InducedRecipe

_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)  # explicit URLs only: 'a//b' is no URL


def recipe_from_induced(induced: "InducedRecipe", domain: str, provenance: dict | None = None) -> Recipe:
    """A real, safe Recipe whose every URL is on `domain` (a registrable domain), or RecipeError with a
    reason that can be fed back to the model. Paths and selectors are compiled here, before any fetch."""
    data = induced.model_dump(exclude={"confidence", "notes"})
    data["provenance"] = provenance or {}
    try:
        recipe = Recipe.model_validate(data)
    except ValidationError as e:
        err = e.errors()[0]
        where = ".".join(str(x) for x in err.get("loc", ()))
        raise RecipeError(f"invalid recipe at {where}: {err.get('msg', 'bad value')}") from None

    f = recipe.fields
    urls = [recipe.list.url] + [v for k, v in recipe.list.headers.items() if k.lower() == "referer"]
    url_is_path = f.url.startswith("$") if recipe.kind == "json_api" else not _is_template(f.url)
    if not url_is_path:
        filled = fill_template(f.url, {"external_id": "1", "title": "t", "location": "l"})
        if not filled.startswith("/") or filled.startswith("//"):  # root-relative stays on the list host
            urls.append(filled)
    if recipe.list.json_body:
        urls += _URL_IN_TEXT.findall(json.dumps(recipe.list.json_body))
    for u in urls:
        if u.startswith("//"):
            u = "https:" + u
        if not u.lower().startswith(("http://", "https://")):
            raise RecipeError(f"recipe URL {u[:100]!r} is not an absolute http(s) URL")
        if registrable_domain(u) != domain:
            raise RecipeError(f"recipe URL {u[:100]!r} is not on {domain}")

    if recipe.kind == "json_api":
        for path in (recipe.items, f.external_id, f.title, f.location, f.posted_at, f.description,
                     f.url if url_is_path else None):
            if path:
                compile_path(path)
        return recipe
    compile_recipe(recipe)  # the CSS subset; anything else is a RecipeError before any fetch
    return recipe
