"""Profile rules: the profile a user edits, the facts they add, kicking off onboarding's AI steps, and the
version rule every area shares (`bump_version`)."""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.errors import Conflict, Invalid, NotFound
from jobfinder.models import Profile, ProfileFact, Task
from jobfinder.repositories import profiles
from jobfinder.storage.documents import DocumentStore, is_tailored
from jobfinder.tasks import ledger


class ProfileIncomplete(Invalid):
    """An AI step needs input the profile doesn't have yet."""

    code = "profile_incomplete"


class StillReading(Conflict):
    code, message = "documents_processing", "Still reading your files and links; try again when they're done"


def bump_version(profile: Profile) -> None:
    """The profile changed in a way scoring reads: matches and letters scored before are now stale."""
    profile.version += 1


async def get_or_create(db: AsyncSession, user_id: int) -> Profile:
    """The user's profile, created empty (version 1) the first time it's asked for."""
    return await profiles.get(db, user_id) or await profiles.create(db, user_id)


async def set_status(db: AsyncSession, user_id: int, status: dict[str, Any]) -> Profile:
    profile = await get_or_create(db, user_id)
    profile.status = status
    bump_version(profile)
    return profile


async def set_background(db: AsyncSession, user_id: int, background: dict[str, Any]) -> Profile:
    profile = await get_or_create(db, user_id)
    profile.background = background
    bump_version(profile)
    return profile


async def list_facts(db: AsyncSession, user_id: int) -> list[ProfileFact]:
    return await profiles.facts(db, user_id)


async def add_fact(db: AsyncSession, user_id: int, fields: dict[str, Any]) -> ProfileFact:
    fact = await profiles.add_fact(db, ProfileFact(user_id=user_id, **fields))
    bump_version(await get_or_create(db, user_id))
    return fact


async def delete_fact(db: AsyncSession, user_id: int, fact_id: int) -> None:
    fact = await profiles.get_fact(db, fact_id)
    if not fact or fact.user_id != user_id:
        raise NotFound("Not found")
    await profiles.delete_fact(db, fact)
    bump_version(await get_or_create(db, user_id))


async def start_extract(db: AsyncSession, store: DocumentStore, user_id: int) -> Task:
    """Queue reading the profile out of the user's own uploads and links (never AI-tailored output)."""
    own = [d for d in store.list(user_id) if not is_tailored(d)]
    if any(d.status == "processing" for d in own):  # its text isn't there yet: it would be left out (#51)
        raise StillReading()
    if not any(d.chars > 0 for d in own):
        raise ProfileIncomplete("Upload a resume or add a link we can read first")
    return await ledger.add(db, "extract_profile", user_id=user_id, dedupe=True)


async def start_followups(db: AsyncSession, user_id: int) -> Task:
    profile = await profiles.get(db, user_id)
    if not profile or not profile.background:
        raise ProfileIncomplete("Save your background first")
    return await ledger.add(db, "followup_questions", user_id=user_id, dedupe=True)
