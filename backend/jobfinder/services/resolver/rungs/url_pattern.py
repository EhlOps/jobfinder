"""Rung 1: the URL itself names a known ATS board (no page fetch)."""
from jobfinder.integrations.directories import board_from_url
from jobfinder.integrations.site_terms import site_for_url
from jobfinder.services.resolver.ladder import BoardHit, ResolveContext


class UrlPatternRung:
    name = "url_pattern"
    order = 1.0

    async def run(self, ctx: ResolveContext) -> BoardHit | None:
        board = board_from_url(ctx.url)
        if not board:
            return None
        site = site_for_url(ctx.url)  # a reviewed custom site knows its company's real name
        return BoardHit(*board, name=site.name if site and site.board == board else None)
