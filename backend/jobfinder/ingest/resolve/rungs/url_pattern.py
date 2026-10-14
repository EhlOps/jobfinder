"""Rung 1: the URL itself names a known ATS board (no page fetch)."""
from jobfinder.ingest.discovery import board_from_url
from jobfinder.ingest.resolve.ladder import BoardHit, ResolveContext


class UrlPatternRung:
    name = "url_pattern"
    order = 1.0

    async def run(self, ctx: ResolveContext) -> BoardHit | None:
        board = board_from_url(ctx.url)
        return BoardHit(*board) if board else None
