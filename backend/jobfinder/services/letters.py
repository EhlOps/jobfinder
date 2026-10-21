"""Cover letters: one current draft per match, drafted by the worker, edited and downloaded by the user.

Moved from letters/service.py (#45).
"""

from jobfinder.core.db import AsyncSession
from jobfinder.core.errors import Conflict, NotFound
from jobfinder.integrations.claude.tasks import TONES, AITasks
from jobfinder.integrations.docx_export import build_docx, filename
from jobfinder.models import CoverLetter, Job, Profile
from jobfinder.repositories import letters as repo
from jobfinder.repositories import matches as matches_repo
from jobfinder.repositories import tasks as tasks_repo
from jobfinder.schemas.common import TaskRef
from jobfinder.schemas.cover_letters import LetterOut, LetterState
from jobfinder.services.matches import owned, require_background
from jobfinder.services.matching.service import job_payload
from jobfinder.tasks import ledger

DEFAULT_TONE = "professional"
VALID_TONES = tuple(TONES)


class NoLetter(NotFound):
    code, message = "no_letter", "No letter yet"


class LetterEdited(Conflict):
    code, message = "letter_edited", "You've edited this letter; redrafting would replace your changes."


def _out(letter: CoverLetter, profile: Profile | None) -> LetterOut:
    return LetterOut(
        content=letter.content, tone=letter.tone, edited=letter.edited, words=len(letter.content.split()),
        stale=letter.profile_version < (profile.version if profile else 1),
        generated_at=letter.generated_at, updated_at=letter.updated_at,
    )


async def _letter(db: AsyncSession, user_id: int, match_id: int) -> tuple[Job, CoverLetter]:
    _, job, _ = await owned(db, user_id, match_id)
    letter = await repo.get(db, user_id, job.id)
    if letter is None:
        raise NoLetter()
    return job, letter


async def state(db: AsyncSession, user_id: int, match_id: int) -> LetterState:
    """The current letter (if any) and the draft task still running for it (if any)."""
    _, job, _ = await owned(db, user_id, match_id)
    letter = await repo.get(db, user_id, job.id)
    pending = await tasks_repo.pending_for(db, user_id, "cover_letter", match_id)
    out = _out(letter, await db.get(Profile, user_id)) if letter else None
    return LetterState(letter=out, pending_task_id=pending)


async def request_draft(db: AsyncSession, user_id: int, match_id: int, tone: str, notes: str, force: bool) -> TaskRef:
    """Queue a (re)draft. Replacing a letter the user edited needs `force`."""
    _, job, _ = await owned(db, user_id, match_id)
    require_background(await db.get(Profile, user_id))
    existing = await repo.get(db, user_id, job.id)
    if existing and existing.edited and not force:
        raise LetterEdited()
    task = await ledger.add(
        db, "cover_letter", {"match_id": match_id, "tone": tone, "notes": notes.strip()}, user_id=user_id, dedupe=True
    )
    return TaskRef(task_id=task.id)


async def save_edits(db: AsyncSession, user_id: int, match_id: int, content: str) -> LetterOut:
    """The user's own text replaces the draft and marks it edited; unchanged text is not an edit."""
    _, letter = await _letter(db, user_id, match_id)
    new = content.replace("\r\n", "\n").strip()
    if new != letter.content:
        letter.content, letter.edited = new, True
        await repo.save(db, letter)
    return _out(letter, await db.get(Profile, user_id))


async def delete(db: AsyncSession, user_id: int, match_id: int) -> None:
    _, job, _ = await owned(db, user_id, match_id)
    await repo.delete(db, user_id, job.id)


async def docx(db: AsyncSession, user_id: int, match_id: int) -> tuple[bytes, str]:
    """(the .docx bytes, its file name)."""
    job, letter = await _letter(db, user_id, match_id)
    return build_docx(letter.content), filename(job.company_name, job.title)


async def generate(db: AsyncSession, ai: AITasks, user_id: int, match_id: int, tone: str, notes: str = "") -> dict:
    """Draft the letter for one of the user's matches and save it as their current draft (the worker's task)."""
    match, job, company = await owned(db, user_id, match_id, "Match not found")
    profile = require_background(await db.get(Profile, user_id))
    posting = job_payload(job, company)
    posting.pop("salary", None)  # never let pay talk leak into the letter
    content = await ai.cover_letter(
        name=(profile.background or {}).get("name", ""),
        background=profile.background,
        facts=await matches_repo.facts(db, user_id),
        job=posting,
        analysis={"strengths": match.reasons, "gaps": match.gaps},
        tone=tone if tone in TONES else DEFAULT_TONE,
        notes=notes,
    )
    await repo.upsert_draft(db, user_id, job.id, content=content, tone=tone, profile_version=profile.version)
    return {"match_id": match_id, "words": len(content.split())}
