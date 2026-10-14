"""Turn a sample JSON response into a json_api recipe (pure, no I/O).

Used by the listing-sniff rung (a JSON endpoint the careers page references) and the Playwright renderer
(a JSON call recorded while the page loads): find the largest array of objects with id- and title-like keys,
then map each field to the first matching key. The caller validates the recipe against the pasted job.
"""
import re
from urllib.parse import parse_qsl, urlsplit

from jobfinder.ingest.recipes import Recipe, RecipeError, parse_recipe
from jobfinder.ingest.recipes.json_api import get_all

MAX_JSON_NODES = 20_000
_PATH_KEY = re.compile(r"[A-Za-z0-9_\-$@]+")

# Candidate keys (compared lower-case without _ or -), best first.
ID_KEYS = ("id", "jobid", "reqid", "requisitionid", "jobreqid", "postingid")
TITLE_KEYS = ("title", "name", "jobtitle", "postingtitle")
URL_KEYS = ("url", "absoluteurl", "applyurl", "canonicalurl", "joburl", "hostedurl", "link")
LOCATION_KEYS = ("location", "locationname", "city", "locations", "primarylocation")
POSTED_KEYS = ("posted", "dateposted", "posteddate", "createdat", "publishedat", "pubdate", "updatedat",
               "postedon")
DESCRIPTION_KEYS = ("description", "content", "descriptionhtml", "summary")
PAGE_KEYS = ("page", "pagenumber", "pageno", "p")
OFFSET_KEYS = ("offset", "from", "start", "skip")
SIZE_KEYS = ("limit", "size", "count", "rows", "pagesize", "perpage")


def _norm_key(k: str) -> str:
    return k.lower().replace("_", "").replace("-", "")


def _find_key(item: dict, wanted: tuple[str, ...], *, scalar: bool = False) -> str | None:
    by_norm = {_norm_key(k): k for k in item if isinstance(k, str)}
    for w in wanted:
        k = by_norm.get(w)
        if k is not None and (not scalar or isinstance(item[k], str | int) and str(item[k]).strip()):
            return k
    return None


def _render_path(segs: list[str]) -> str | None:
    out = "$"
    for s in segs:
        if s == "*":
            out += "[*]"
        elif _PATH_KEY.fullmatch(s):
            out += f".{s}"
        elif s and '"' not in s:
            out += f'["{s}"]'
        else:
            return None
    return out


def _array_paths(data) -> list[list[str]]:
    """Paths (with '*' for list hops) to every list of objects in a JSON document, bounded."""
    out, seen, stack, nodes = [], set(), [(data, [])], 0
    while stack and nodes < MAX_JSON_NODES:
        node, path = stack.pop()
        nodes += 1
        if len(path) > 8:
            continue
        if isinstance(node, dict):
            stack.extend((v, [*path, str(k)]) for k, v in node.items() if isinstance(v, dict | list))
        elif isinstance(node, list) and node:
            if any(isinstance(x, dict) for x in node[:50]) and tuple(path) not in seen:
                seen.add(tuple(path))
                out.append(path)
            stack.extend((x, [*path, "*"]) for x in node[:50] if isinstance(x, dict | list))
    return out


def job_items(data) -> tuple[str, list[dict]] | None:
    """(items path, items) for the largest array of objects with a title-like key and an id- (or, failing
    that, a URL-) like key."""
    best: tuple[str, list[dict]] | None = None
    for segs in _array_paths(data):
        path = _render_path([*segs, "*"])
        if path is None:
            continue
        good = [x for x in get_all(data, path) if isinstance(x, dict) and _find_key(x, TITLE_KEYS, scalar=True)
                and (_find_key(x, ID_KEYS, scalar=True) or _find_key(x, URL_KEYS, scalar=True))]
        if good and (best is None or len(good) > len(best[1])):
            best = (path, good)
    return best


def _majority_key(items: list[dict], wanted: tuple[str, ...], *, scalar: bool = False) -> str | None:
    sample = items[:20]
    counts: dict[str, int] = {}
    for x in sample:
        k = _find_key(x, wanted, scalar=scalar)
        if k is not None:
            counts[k] = counts.get(k, 0) + 1
    k = max(counts, key=counts.get, default=None)
    return k if k is not None and counts[k] * 2 >= len(sample) else None


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else int(v) if isinstance(v, str) and v.isdigit() \
        else None


def pagination_for(url: str, json_body: dict | None = None) -> dict:
    """Page through the endpoint when its query (GET) or top-level body (POST) already has a page or offset
    parameter; the size parameter gives the offset step. Paging always starts at the first page (0 or 1),
    whatever page the sample happened to be."""
    params = dict(json_body) if json_body is not None else dict(parse_qsl(urlsplit(url).query))
    q = {_norm_key(k): (k, _int(v)) for k, v in params.items() if isinstance(k, str) and _int(v) is not None}
    for name in PAGE_KEYS:
        if name in q:
            return {"kind": "page", "param": q[name][0], "start": min(q[name][1], 1), "step": 1}
    size = next((q[n][1] for n in SIZE_KEYS if n in q and q[n][1] > 0), None)
    for name in OFFSET_KEYS:
        if size and name in q:
            return {"kind": "offset", "param": q[name][0], "start": 0, "step": min(size, 1000)}
    return {"kind": "none"}


def _url_template(pasted_url: str, ids: list[str]) -> str | None:
    """The pasted job URL with its job id (a whole token of it) replaced by {external_id}. When the id is
    a whole path segment, later path segments (usually that job's title slug) are dropped: '/jobs/12/eng'
    -> '/jobs/{external_id}'. An id inside a slug ('/jobs/12-eng') keeps the slug, which most sites ignore."""
    base = pasted_url.split("#")[0]
    tokens = set(re.split(r"[/?&=#._-]+", base))
    hit = next((i for i in ids if len(i) >= 4 and i in tokens), None)
    if hit is None:
        return None
    m = None
    for m in re.finditer(rf"(?<![A-Za-z0-9]){re.escape(hit)}(?![A-Za-z0-9])", base):
        pass
    if m is None:
        return None
    head, tail = base[:m.start()], base[m.end():]
    query = urlsplit(base).query
    if "?" not in head and head.endswith("/") and (not tail or tail[0] in "/?"):
        # drop the title slug after an id-only segment
        tail = ("/" if tail.startswith("/") else "") + (f"?{query}" if query else "")
    return head + "{external_id}" + tail


def explain_json_sample(url: str, method: str, body: object, json_body: dict | None = None, *,
                        pasted_url: str | None = None) -> tuple[Recipe | None, str]:
    """json_recipe_from_sample plus the reason when there's no recipe."""
    found = job_items(body)
    if found is None:
        return None, "no array of jobs with id and title"
    path, items = found
    keys = {name: _majority_key(items, wanted, scalar=scalar) for name, wanted, scalar in (
        ("external_id", ID_KEYS, True), ("title", TITLE_KEYS, True), ("url", URL_KEYS, True),
        ("location", LOCATION_KEYS, False), ("posted_at", POSTED_KEYS, True),
        ("description", DESCRIPTION_KEYS, True))}
    if not keys["external_id"]:  # no id field: a job's own URL identifies it
        keys["external_id"] = keys["url"]
    if not keys["external_id"] or not keys["title"]:
        return None, "items have no consistent id/title keys"
    fields = {k: _render_path([v]) for k, v in keys.items() if v}
    if None in fields.values():
        return None, "unusual key names"
    if "url" not in fields:  # no link in the data: build it from the pasted URL around the job's id
        ids = [str(x[keys["external_id"]]) for x in items if keys["external_id"] in x]
        template = _url_template(pasted_url or "", ids)
        if template is None:
            return None, "items have no URL and the pasted URL doesn't contain a job id"
        fields["url"] = template
    method = (method or "GET").upper()
    req: dict = {"method": method, "url": url}
    if method == "POST":
        req["json_body"] = json_body if json_body is not None else {}
    try:
        recipe = parse_recipe({"kind": "json_api", "list": req, "items": path, "fields": fields,
                               "pagination": pagination_for(url, json_body if method == "POST" else None)})
    except RecipeError as e:
        return None, str(e)
    return recipe, ""


def json_recipe_from_sample(url: str, method: str, body: object, json_body: dict | None = None, *,
                            pasted_url: str | None = None) -> Recipe | None:
    """A json_api recipe for the endpoint `url` (called with `method` / `json_body`) whose response was
    `body`, or None if it doesn't look like a job list. `pasted_url` lets a job URL be templated from the
    pasted link when the items carry no URL of their own. Pure: the caller validates the recipe."""
    return explain_json_sample(url, method, body, json_body, pasted_url=pasted_url)[0]
