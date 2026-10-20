"""html recipes: fetch a server-rendered job listing page, page through it, and read each job with CSS
selectors.

Selectors are a small, safe CSS subset matched directly against the lxml tree (nothing is translated to or
evaluated as XPath): type `a` / `*`, `.class`, `#id`, `[attr]`, `[attr=v]` (also `~= ^= $= *=`), the
descendant (space) and child (`>`) combinators, and `:scope` (the item itself) at the start. A field may
end in `@attr` to read an attribute instead of the text: `a.job-title@href`, `time@datetime`, `@href`.
Anything else is a RecipeError before any request is made.
"""
import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
import lxml.etree
import lxml.html

from jobfinder.ingest.base import JobPosting
from jobfinder.ingest.http_safety import TEXT_TYPES, SafeResponse, safe_fetch
from jobfinder.integrations.recipes import (
    MAX_ITEMS,
    RECIPE_RUNNERS,
    Recipe,
    RecipeError,
    fill_template,
)
from jobfinder.integrations.recipes.json_api import make_posting, page_request, parse_date
from jobfinder.integrations.robots import PoliteFetcher

PAGE_MAX_BYTES = 3_000_000
MAX_STEPS = 12  # compounds per selector
_IDENT = r"-?[A-Za-z_][A-Za-z0-9_-]*"
_ATTR_NAME = r"[A-Za-z_][A-Za-z0-9_:.-]*"
_VALUE = r"""(?:"([^"\\\]\x00-\x1f]*)"|'([^'\\\]\x00-\x1f]*)'|(""" + _IDENT + r"))"
_SIMPLE = re.compile(
    r"\.(" + _IDENT + r")"                                  # .class
    r"|#(" + _IDENT + r")"                                  # #id
    r"|\[\s*(" + _ATTR_NAME + r")\s*(?:([~^$*]?=)\s*" + _VALUE + r"\s*)?\]"  # [attr op value]
)
_TAG = re.compile(r"\*|" + _IDENT)
_COMBINATOR = re.compile(r"\s*>\s*|\s+")
_FIELD_ATTR = re.compile(r"@(" + _ATTR_NAME + r")$")


@dataclass
class Compound:
    tag: str | None = None  # None = any element
    ids: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    attrs: list[tuple[str, str | None, str]] = field(default_factory=list)  # (name, op, value)

    def matches(self, el) -> bool:
        if not isinstance(el.tag, str):  # comments, processing instructions
            return False
        if self.tag is not None and el.tag.lower() != self.tag:
            return False
        if self.ids and any(el.get("id") != i for i in self.ids):
            return False
        if self.classes:
            have = set((el.get("class") or "").split())
            if not all(c in have for c in self.classes):
                return False
        return all(_attr_ok(el.get(name), op, value) for name, op, value in self.attrs)


def _attr_ok(v: str | None, op: str | None, value: str) -> bool:
    if v is None:
        return False
    if op is None:
        return True
    if op == "=":
        return v == value
    if op == "~=":
        return value in v.split()
    if not value:  # [a^=""] etc. match nothing, as in CSS
        return False
    return v.startswith(value) if op == "^=" else v.endswith(value) if op == "$=" else value in v


Selector = list[tuple[str, Compound]]  # (combinator to reach it: " " descendant | ">" child, compound)


def compile_selector(sel: str) -> Selector:
    """'ul.jobs > li a[href*="/jobs/"]' -> steps. ':scope' alone (or '') selects the context element.
    Raises RecipeError on anything outside the subset."""
    s = (sel or "").strip()
    steps: Selector = []
    comb = " "
    if s.startswith(":scope"):
        s = s[len(":scope"):]
        if s and not (s[0].isspace() or s[0] == ">"):
            raise RecipeError(f"unsupported selector: {sel!r}")
        s = s.strip()
        if s.startswith(">"):
            comb, s = ">", s[1:].strip()
        if not s:
            return []
    elif not s:
        return []
    pos = 0
    while True:
        compound = Compound()
        m = _TAG.match(s, pos)
        if m:
            compound.tag = None if m.group() == "*" else m.group().lower()
            pos = m.end()
        start, had_tag = pos, m is not None
        while (m := _SIMPLE.match(s, pos)):
            cls, ident, name, op, dq, sq, bare = m.groups()
            if cls:
                compound.classes.append(cls)
            elif ident:
                compound.ids.append(ident)
            else:
                compound.attrs.append((name.lower(), op, next((x for x in (dq, sq, bare) if x is not None), "")))
            pos = m.end()
        if not had_tag and pos == start:
            raise RecipeError(f"unsupported selector: {sel!r}")
        steps.append((comb, compound))
        if len(steps) > MAX_STEPS:
            raise RecipeError(f"selector too long: {sel!r}")
        if pos == len(s):
            return steps
        m = _COMBINATOR.match(s, pos)
        if not m or m.end() == len(s):
            raise RecipeError(f"unsupported selector: {sel!r}")
        comb = ">" if ">" in m.group() else " "
        pos = m.end()


def select(root, steps: Selector) -> list:
    """Elements under `root` matching the compiled selector, in document order (root itself if no steps)."""
    current = [root]
    for comb, compound in steps:
        found: dict = {}
        if comb == " " and len(current) > 1:  # a node inside another one adds nothing: keep it O(n)
            mine = set(current)
            current = [n for n in current if not any(a in mine for a in n.iterancestors())]
        for node in current:
            for el in (node.iterdescendants() if comb == " " else node.iterchildren()):
                if el not in found and compound.matches(el):
                    found[el] = None
        current = list(found)
        if not current:
            return []
    if len(current) > 1 and len(steps) > 1:  # nested matches can come out of order
        order = {el: i for i, el in enumerate(root.iter())}
        current.sort(key=lambda el: order.get(el, 0))
    return current


@dataclass
class FieldSpec:
    steps: Selector
    attr: str | None


def compile_field(spec: str | None) -> FieldSpec | None:
    if spec is None:
        return None
    sel, attr = spec.strip(), None
    m = _FIELD_ATTR.search(sel)
    if m and sel[:m.start()].count("[") == sel[:m.start()].count("]"):  # not inside [attr=...]
        sel, attr = sel[:m.start()], m.group(1).lower()
    return FieldSpec(compile_selector(sel), attr)


def _text(el) -> str:
    return " ".join(" ".join(el.itertext()).split())


def field_value(item, spec: FieldSpec | None, *, as_html: bool = False) -> str:
    if spec is None:
        return ""
    found = select(item, spec.steps) if spec.steps else [item]
    if not found:
        return ""
    el = found[0]
    if spec.attr:
        return (el.get(spec.attr) or "").strip()
    if as_html:
        return lxml.html.tostring(el, encoding="unicode", with_tail=False)
    return _text(el)


def _is_template(url_field: str) -> bool:
    return "{" in url_field or url_field.startswith(("http://", "https://"))


@dataclass
class CompiledRecipe:
    items: Selector
    external_id: FieldSpec
    title: FieldSpec
    url: FieldSpec | None  # None: fields.url is a template
    location: FieldSpec | None
    posted_at: FieldSpec | None
    description: FieldSpec | None


def compile_recipe(recipe: Recipe) -> CompiledRecipe:
    f = recipe.fields
    items = compile_selector(recipe.items)
    if not items:
        raise RecipeError("items selector is empty")
    return CompiledRecipe(
        items=items, external_id=compile_field(f.external_id), title=compile_field(f.title),
        url=None if _is_template(f.url) else compile_field(f.url), location=compile_field(f.location),
        posted_at=compile_field(f.posted_at), description=compile_field(f.description),
    )


_META_CHARSET = re.compile(rb"""<meta[^>]{0,200}?charset=["']?([A-Za-z0-9_.:-]{1,40})""", re.IGNORECASE)


def page_text(r: SafeResponse) -> str:
    """A fetched page as text: the Content-Type charset, else a <meta charset> in the page, else UTF-8."""
    ctype = r.headers.get("content-type", "") if r.headers is not None else ""
    m = re.search(r"charset=([A-Za-z0-9_.:-]+)", ctype)
    enc = m.group(1) if m else None
    if enc is None and (meta := _META_CHARSET.search(r.body[:4096])):
        enc = meta.group(1).decode()
    try:
        return r.body.decode(enc or "utf-8", errors="replace")
    except LookupError:
        return r.body.decode("utf-8", errors="replace")


def parse_document(page: str):
    try:
        return lxml.html.document_fromstring(
            page.encode("utf-8"), parser=lxml.html.HTMLParser(encoding="utf-8", no_network=True,
                                                              remove_comments=True))
    except (lxml.etree.ParserError, ValueError):  # empty or comment-only document
        return None


def _id_from(value: str, base_url: str) -> str:
    """An id read from a link becomes its path (+query), so http/https and host variants agree."""
    if value.startswith(("/", "http://", "https://", "?")):
        p = urlsplit(urljoin(base_url, value))
        if p.scheme in ("http", "https"):
            return p.path + (f"?{p.query}" if p.query else "")
    return value


def map_items(page: str, recipe: Recipe, compiled: CompiledRecipe, slug: str, company_name: str,
              base_url: str) -> list[JobPosting]:
    doc = parse_document(page)
    if doc is None:
        return []
    out = []
    for item in select(doc, compiled.items)[:MAX_ITEMS]:
        eid = _id_from(field_value(item, compiled.external_id), base_url)
        title = field_value(item, compiled.title)
        if not eid or not title:
            continue
        location = field_value(item, compiled.location)
        if compiled.url is None:
            raw = fill_template(recipe.fields.url, {"external_id": eid, "title": title, "location": location})
        else:
            raw = field_value(item, compiled.url)
        out.append(make_posting(
            slug, company_name, eid=eid, title=title, url=urljoin(base_url, raw.strip()) if raw else "",
            location=location, description=field_value(item, compiled.description, as_html=True),
            posted_at=parse_date(field_value(item, compiled.posted_at) or None),
        ))
    return out


def _map_page(r: SafeResponse, recipe: Recipe, compiled: CompiledRecipe, slug: str, company_name: str
              ) -> list[JobPosting]:
    return map_items(page_text(r), recipe, compiled, slug, company_name, r.url)


async def run_html(client: httpx.AsyncClient, recipe: Recipe, slug: str, company_name: str
                   ) -> list[JobPosting]:
    compiled = compile_recipe(recipe)  # refuse a bad selector before any request
    robots = PoliteFetcher(client)
    pages = 1 if recipe.pagination.kind == "none" else recipe.pagination.max_pages
    out: list[JobPosting] = []
    seen: set[str] = set()
    for page in range(pages):
        url, body = page_request(recipe, page)
        r = await safe_fetch(client, url, method=recipe.list.method, headers=recipe.list.headers or None,
                             json=body, accept=TEXT_TYPES, max_bytes=PAGE_MAX_BYTES, robots=robots)
        added = 0
        # decoding + lxml parsing a page is CPU-bound: keep it off the event loop
        for p in await asyncio.to_thread(_map_page, r, recipe, compiled, slug, company_name):
            if p.external_id not in seen:
                seen.add(p.external_id)
                out.append(p)
                added += 1
        if not added or len(out) >= MAX_ITEMS:  # empty or repeating page: we've reached the end
            break
    return out[:MAX_ITEMS]


RECIPE_RUNNERS["html"] = run_html
