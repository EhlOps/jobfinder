"""Editing boards from the CLI (`jobfinder boards list|set`): the listing and the per-board edit.

Adding a board goes through services.board_validation.add_board (it validates with a live fetch); re-enabling goes
through admin.enable_source, the same rule as Admin › Sources. Nothing here commits.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.errors import Invalid, NotFound
from jobfinder.models import Company
from jobfinder.repositories import companies
from jobfinder.services.admin import enable_source
from jobfinder.services.ingestion import VALID_SIZES


class NoSuchBoard(NotFound):
    code = "no_such_board"


async def listing(db: AsyncSession, *, ats: str | None = None, origin: str | None = None) -> list[Company]:
    return await companies.listing(db, ats=ats, origin=origin)


async def update(
    db: AsyncSession,
    board_id: int,
    *,
    name: str | None = None,
    prestige: int | None = None,
    size: str | None = None,
    industry: str | None = None,
    sponsors_visas: bool | None = None,
    enabled: bool | None = None,
) -> tuple[Company, dict]:
    """Apply the given fields (None leaves one as it is). Returns the board and what was set."""
    name = name.strip() if name is not None else None
    if name is not None and not 1 <= len(name) <= 200:
        raise Invalid("invalid name (must be 1-200 characters)")
    if industry is not None and len(industry) > 100:
        raise Invalid("invalid industry (at most 100 characters)")
    if prestige is not None and not 1 <= prestige <= 5:
        raise Invalid("invalid prestige (must be 1-5)")
    if size is not None and size not in VALID_SIZES:
        raise Invalid(f"invalid size (one of {', '.join(sorted(VALID_SIZES))})")
    company = await companies.get(db, board_id)
    if company is None:
        raise NoSuchBoard(f"No board {board_id}")
    changes = {k: v for k, v in (
        ("name", name), ("prestige_tier", prestige), ("size", size),
        ("industry", industry), ("sponsors_visas", sponsors_visas),
    ) if v is not None}
    for k, v in changes.items():
        setattr(company, k, v)
    if enabled is False:
        company.enabled = False
    elif enabled:
        await enable_source(db, company.id)  # the admin page's re-enable rule
    if enabled is not None:
        changes["enabled"] = enabled
    await db.flush()
    return company, changes
