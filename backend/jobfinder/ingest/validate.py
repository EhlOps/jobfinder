"""Board validation: a live fetch must succeed and return at least one posting before a board is added."""
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.ingest.sources import ATS_FETCHERS, PROBE_FETCHERS
from jobfinder.models import Company

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
ORIGINS = ("seed", "discovered")


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    job_count: int = 0


async def validate_board(
    ats: str, slug: str, client: httpx.AsyncClient | None = None, list_only: bool = False
) -> ValidationResult:
    """Fetch the board through its ATS fetcher (list_only: the cheap probe without per-posting detail requests).
    Never raises: failures come back as ok=False with a reason."""
    fetcher = (PROBE_FETCHERS if list_only else ATS_FETCHERS).get(ats)
    if fetcher is None:
        return ValidationResult(False, f"unknown ATS '{ats}'")
    if not SLUG_RE.match(slug or ""):
        return ValidationResult(False, f"invalid slug '{slug}'")
    own = client is None
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=True)
    try:
        postings = await fetcher(client, slug, slug)
    except httpx.HTTPStatusError as e:
        return ValidationResult(False, f"HTTP {e.response.status_code} from {ats} board '{slug}'")
    except httpx.HTTPError as e:
        return ValidationResult(False, f"request failed: {type(e).__name__}: {e}"[:300])
    except Exception as e:  # malformed JSON / unexpected shape
        return ValidationResult(False, f"malformed response: {type(e).__name__}: {e}"[:300])
    finally:
        if own:
            await client.aclose()
    if not postings:
        return ValidationResult(False, "board has no postings", 0)
    return ValidationResult(True, "", len(postings))


async def add_board(
    session: AsyncSession,
    ats: str,
    slug: str,
    name: str,
    origin: str = "discovered",
    client: httpx.AsyncClient | None = None,
    validation: ValidationResult | None = None,
) -> tuple[Company | None, str]:
    """Validate (unless a `validation` result is supplied) then insert a board.
    Returns (company, reason); company is None when refused or a duplicate."""
    name = (name or "").strip()
    if not name or len(name) > 200:
        return None, "invalid name (must be 1-200 characters)"
    if origin not in ORIGINS:
        return None, f"invalid origin '{origin}'"
    existing = await session.scalar(sa.select(Company).where(Company.ats == ats, Company.slug == slug))
    if existing:
        return None, "duplicate: board already exists"
    result = validation or await validate_board(ats, slug, client)
    if not result.ok:
        return None, result.reason
    company = Company(name=name, ats=ats, slug=slug, origin=origin, validated_at=datetime.now(UTC))
    session.add(company)
    try:
        await session.commit()
    except IntegrityError:  # lost a race with another insert of the same board
        await session.rollback()
        return None, "duplicate: board already exists"
    return company, f"added ({result.job_count} postings)"
