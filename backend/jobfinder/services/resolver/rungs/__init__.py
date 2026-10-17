"""The resolver ladder, cheapest rung first. Add a rung by appending an instance to _ALL with its `order`:
1 url_pattern · 2 embedded ATS · 3 JSON-LD · 4 listing sniff · 4.5 Playwright render · 5 LLM recipe."""
from jobfinder.services.resolver.ladder import Rung
from jobfinder.services.resolver.rungs.embedded import EmbeddedRung
from jobfinder.services.resolver.rungs.jsonld import JsonLdRung
from jobfinder.services.resolver.rungs.listing import ListingRung
from jobfinder.services.resolver.rungs.llm import LlmRung
from jobfinder.services.resolver.rungs.rendered import RenderedRung
from jobfinder.services.resolver.rungs.url_pattern import UrlPatternRung

_ALL: list[Rung] = [
    UrlPatternRung(),
    EmbeddedRung(),
    JsonLdRung(),
    ListingRung(),
    LlmRung(),
    RenderedRung(),
]

RUNGS: list[Rung] = sorted(_ALL, key=lambda r: r.order)
