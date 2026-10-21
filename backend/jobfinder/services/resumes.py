"""Tailored resumes: one per match, kept in the file store, built from the candidate's own facts.

The worker tailors (`tailor`, `save`); the user reads, edits, deletes and downloads. Edits are marked
with a tag so a later regeneration never silently replaces them. Moved from resumes/service.py (#45).
"""
import json
import re
from datetime import datetime

from pydantic import ValidationError

from jobfinder.core.db import AsyncSession
from jobfinder.core.errors import Conflict, NotFound
from jobfinder.integrations.claude.schemas import TailoredResume
from jobfinder.integrations.claude.tasks import AITasks
from jobfinder.models import Job, Profile
from jobfinder.repositories import matches as matches_repo
from jobfinder.repositories import tasks as tasks_repo
from jobfinder.resumes import keywords
from jobfinder.resumes.export import render_docx, render_pdf, resume_to_text
from jobfinder.schemas.common import DOCX, PDF, TaskRef
from jobfinder.schemas.resumes import CoverageOut, ResumeOut, ResumeState, TailoredResumeOut
from jobfinder.services.matches import owned, require_background
from jobfinder.services.matching.service import job_payload
from jobfinder.storage.documents import MATCH_TAG, DocumentMeta, DocumentStore, is_tailored
from jobfinder.storage.documents import NotFound as FileGone
from jobfinder.tasks import ledger

EDITED_TAG = "edited"
EDITED_MSG = "You edited this resume, so it was not regenerated. Reload and choose overwrite to replace your edits."


class NoResume(NotFound):
    code, message = "no_resume", "No resume yet"


class ResumeEdited(Conflict):
    code, message = "resume_edited", "You've edited this resume; regenerating would replace your changes."


class ResumeReplaced(Conflict):
    code, message = "resume_replaced", "The resume was just regenerated; reload and edit again."


def match_tags(match_id: int) -> dict[str, str]:
    return {MATCH_TAG: str(match_id)}


def source_documents(store: DocumentStore, user_id: int) -> list[tuple[str, str]]:
    """Uploads and links the candidate supplied; earlier tailored output is not a source of facts."""
    return [
        (meta.filename, store.read_text(user_id, meta.id))
        for meta in store.list(user_id)
        if not is_tailored(meta) and meta.chars > 0
    ]


# --- the user's side (API) ---

def _current(store: DocumentStore, user_id: int, match_id: int) -> tuple[DocumentMeta, TailoredResumeOut] | None:
    metas = store.list(user_id, kind="resume", tags=match_tags(match_id))
    if not metas:
        return None
    meta = max(metas, key=lambda m: m.created_at)
    try:  # the file holds claude.schemas.TailoredResume JSON, which has the same fields (test_architecture checks)
        return meta, TailoredResumeOut.model_validate(json.loads(store.read_text(user_id, meta.id)))
    except (ValueError, ValidationError, FileGone):
        return None  # unreadable file: treat as no resume so the user can regenerate


def _require_current(store: DocumentStore, user_id: int, match_id: int) -> tuple[DocumentMeta, TailoredResumeOut]:
    found = _current(store, user_id, match_id)
    if found is None:
        raise NoResume()
    return found


def _out(job: Job, meta: DocumentMeta, resume: TailoredResumeOut) -> ResumeOut:
    terms = keywords.extract_terms(f"{job.title}\n{job.description_text}")
    cov = keywords.coverage(resume_to_text(resume), terms, job.source)  # the ATS comes from the job's source
    return ResumeOut(
        content=resume, edited=meta.tags.get(EDITED_TAG) == "1",
        coverage=CoverageOut(percent=cov.percent, covered=cov.covered, missing=cov.missing, loose=cov.loose, ats=cov.ats, notes=cov.notes),
        generated_at=datetime.fromisoformat(meta.created_at), updated_at=datetime.fromisoformat(meta.fetched_at or meta.created_at),
    )


async def state(db: AsyncSession, store: DocumentStore, user_id: int, match_id: int) -> ResumeState:
    """The current resume with its keyword coverage (if any), and the tailoring task still running (if any)."""
    _, job, _ = await owned(db, user_id, match_id)
    found = _current(store, user_id, match_id)
    pending = await tasks_repo.pending_for(db, user_id, "resume_tailor", match_id)
    return ResumeState(resume=_out(job, *found) if found else None, pending_task_id=pending)


async def request_tailor(db: AsyncSession, store: DocumentStore, user_id: int, match_id: int, force: bool) -> TaskRef:
    """Queue a (re)tailoring. Replacing a resume the user edited needs `force`."""
    await owned(db, user_id, match_id)
    require_background(await db.get(Profile, user_id))
    if not force and _edited(store, user_id, match_id):  # the worker's save() makes the same check
        raise ResumeEdited()
    payload = {"match_id": match_id, "force": True} if force else {"match_id": match_id}
    task = await ledger.add(db, "resume_tailor", payload, user_id=user_id, dedupe=True)
    return TaskRef(task_id=task.id)


async def save_edits(
    db: AsyncSession, store: DocumentStore, user_id: int, match_id: int, content: TailoredResumeOut,
) -> ResumeOut:
    """The user's version replaces the file and is tagged edited; an unchanged resume is not an edit."""
    _, job, _ = await owned(db, user_id, match_id)
    meta, old = _require_current(store, user_id, match_id)
    if content != old:
        text = json.dumps(content.model_dump(), indent=1)
        try:
            meta = store.replace_text(user_id, meta.id, text, fetched=True)
            meta = store.set_tags(user_id, meta.id, {**match_tags(match_id), EDITED_TAG: "1"})
        except FileGone:  # a regeneration replaced the document while we were saving
            raise ResumeReplaced() from None
    return _out(job, meta, content)


async def delete(db: AsyncSession, store: DocumentStore, user_id: int, match_id: int) -> None:
    await owned(db, user_id, match_id)
    for meta in store.list(user_id, kind="resume", tags=match_tags(match_id)):
        try:
            store.delete(user_id, meta.id)
        except FileGone:
            pass  # already gone


async def export(
    db: AsyncSession, store: DocumentStore, user_id: int, match_id: int, format: str,
) -> tuple[bytes, str, str]:
    """(the file's bytes, its name, its media type) for format "docx" or "pdf"."""
    _, job, _ = await owned(db, user_id, match_id)
    _, resume = _require_current(store, user_id, match_id)
    base = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ._-]+", "", f"Resume - {job.company_name} - {job.title}").strip())
    name = f"{base[:120]}.{format}"
    return (render_docx(resume), name, DOCX) if format == "docx" else (render_pdf(resume), name, PDF)


# --- the worker's side ---

async def tailor(db: AsyncSession, ai: AITasks, store: DocumentStore, user_id: int, match_id: int, force: bool = False) -> dict:
    """Tailor the resume to one of the user's matches and keep it in the file store (the worker's task)."""
    _, job, company = await owned(db, user_id, match_id, "Match not found")
    if not force and _edited(store, user_id, match_id):  # fail before the slow AI call; save() re-checks
        raise ValueError(EDITED_MSG)
    profile = require_background(await db.get(Profile, user_id))
    posting = job_payload(job, company)
    posting.pop("salary", None)  # pay talk has no place on a resume
    resume = await ai.tailor_resume(
        name=(profile.background or {}).get("name", ""),
        background=profile.background,
        facts=await matches_repo.facts(db, user_id),
        documents=source_documents(store, user_id),
        job=posting,
    )
    return save(store, user_id, match_id, resume, f"{job.company_name} {job.title}", force=force)


def _edited(store: DocumentStore, user_id: int, match_id: int) -> bool:
    """Whether the current (newest) resume is user-edited; the same document the API's 409 guard looks at."""
    metas = store.list(user_id, kind="resume", tags=match_tags(match_id))
    return bool(metas) and max(metas, key=lambda m: m.created_at).tags.get(EDITED_TAG) == "1"


def save(
    store: DocumentStore, user_id: int, match_id: int, resume: TailoredResume, label: str = "", force: bool = False,
) -> dict:
    """Store the tailored resume (JSON text) as kind 'resume' tagged match:<id>; one per match, newest wins.

    The user may have edited the resume since this generation was queued (the API only checks at enqueue time),
    so re-check right before writing and refuse to replace edits unless the request was forced."""
    tags = match_tags(match_id)
    if not force and _edited(store, user_id, match_id):
        raise ValueError(EDITED_MSG)
    meta, _ = store.put(
        user_id, "resume", f"Tailored resume - {label or match_id}.json",
        text=json.dumps(resume.model_dump(), indent=1), content_type="application/json", tags=tags,
    )
    for old in store.list(user_id, kind="resume", tags=tags):  # only after the new one is safely written
        if old.id != meta.id and (force or old.tags.get(EDITED_TAG) != "1"):  # an edit landing mid-save survives
            store.delete(user_id, old.id)
    return {"match_id": match_id, "document_id": meta.id}
