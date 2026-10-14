"""The resolver ladder, cheapest rung first. Add a rung by appending an instance to _ALL with its `order`:
1 url_pattern · 2 embedded ATS · 3 JSON-LD · 4 listing sniff · 4.5 Playwright render · 5 LLM recipe."""
from jobfinder.ingest.resolve.ladder import Rung
from jobfinder.ingest.resolve.rungs.embedded import EmbeddedRung
from jobfinder.ingest.resolve.rungs.jsonld import JsonLdRung
from jobfinder.ingest.resolve.rungs.listing import ListingRung
from jobfinder.ingest.resolve.rungs.llm import LlmRung
from jobfinder.ingest.resolve.rungs.rendered import RenderedRung
from jobfinder.ingest.resolve.rungs.url_pattern import UrlPatternRung

_ALL: list[Rung] = [
    UrlPatternRung(),
    EmbeddedRung(),
    JsonLdRung(),
    ListingRung(),
    LlmRung(),
    RenderedRung(),
]

RUNGS: list[Rung] = sorted(_ALL, key=lambda r: r.order)
