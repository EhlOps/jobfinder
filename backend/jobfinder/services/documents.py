"""Uploaded documents: what may be uploaded, reading its text, and the user's files in the document store.

The store holds the files. An upload is saved as "processing" and read by worker-priority's parse_document task
(`parse`), the upload's only database write (#51). The store's NotFound becomes the API's 404, and every failure keeps the status and wording the
routers always answered with.
"""
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from jobfinder.core.clock import utcnow
from jobfinder.core.config import settings
from jobfinder.core.db import AsyncSession
from jobfinder.core.errors import Invalid, NotFound, TooLarge, Unsupported
from jobfinder.profile.formats import ALLOWED_EXTENSIONS, CONTENT_TYPES
from jobfinder.schemas.documents import DocumentOut, DocumentUpload
from jobfinder.storage import documents as storage
from jobfinder.storage.documents import (
    MATCH_TAG,
    DocumentMeta,
    DocumentStore,
    StoreError,
    is_tailored,
)
from jobfinder.tasks import ledger

log = logging.getLogger("jobfinder.documents")
KINDS = {"resume", "portfolio", "other"}
UNREADABLE = "Could not read that file"


def get(store: DocumentStore, user_id: int, doc_id: str) -> DocumentMeta:
    try:
        return store.get(user_id, doc_id)
    except storage.NotFound:
        raise NotFound("Not found") from None


async def list_documents(
    db: AsyncSession, store: DocumentStore, user_id: int, kind: str | None, tags: list[str],
) -> list[DocumentMeta]:
    """`tags` are "key:value" filters. Tailored resumes and links only show up when asked for."""
    wanted = {}
    for t in tags:
        key, sep, value = t.partition(":")
        if not sep:
            raise Invalid("tag must look like key:value")
        wanted[key] = value
    docs = store.list(user_id, kind=kind, tags=wanted)
    if MATCH_TAG not in wanted:  # tailored resumes belong to their match; ask for them by tag
        docs = [d for d in docs if not is_tailored(d)]
    if kind is None:
        docs = [d for d in docs if d.kind != "link"]  # links have their own endpoint
    return await settle(db, store, user_id, docs)


# ---------------------------------------------------------------- reads and fetches that never finish
# A document or link is "processing" until its task (parse_document, fetch_link) records the outcome. When
# that task fails for good instead (out of attempts, or an unexpected error), tasks.failures calls
# `unfinished`. When there is no task at all (the upload's request failed to commit after the store
# wrote the file, or the row was lost), `settle` notices on the next listing.

TASKS = {"link": "fetch_link"}  # the task that reads each kind of item; every other kind is parse_document
UNFINISHED = {
    "parse_document": "We couldn't finish reading this file; upload it again",
    "fetch_link": "We couldn't finish fetching this link; refresh it to try again",
}
# Longer than a request takes from storing an item to committing its task, so `settle` never races one.
NO_TASK_GRACE = timedelta(minutes=2)


def task_for(meta: DocumentMeta) -> str:
    return TASKS.get(meta.kind, "parse_document")


def unfinished(store: DocumentStore, user_id: int, doc_id: str) -> DocumentMeta | None:
    """Mark an item still processing as error (its text is kept), so nothing waits on it any longer."""
    try:
        meta = store.get(user_id, doc_id)
        if meta.status != "processing":
            return meta
        return store.mark_error(user_id, doc_id, UNFINISHED[task_for(meta)])
    except (storage.NotFound, FileNotFoundError):  # deleted meanwhile
        return None


async def settle(db: AsyncSession, store: DocumentStore, user_id: int, docs: list[DocumentMeta]) -> list[DocumentMeta]:
    """`docs`, with any item processing for NO_TASK_GRACE with no queued or running task marked error."""
    waiting = {d.id: ledger.dedupe_key(task_for(d), user_id, {"doc_id": d.id}) for d in docs if d.status == "processing"}
    if not waiting:
        return docs
    active, cutoff = await ledger.active_keys(db, list(waiting.values())), utcnow() - NO_TASK_GRACE
    out = []
    for d in docs:
        if d.id in waiting and waiting[d.id] not in active:
            try:
                stale = store.changed_at(user_id, d.id) < cutoff
            except storage.NotFound:
                continue  # deleted meanwhile
            if stale:
                log.warning("%s %s of user %s was left processing with no task", d.kind, d.id, user_id)
                if (d := unfinished(store, user_id, d.id)) is None:
                    continue  # deleted meanwhile
        out.append(d)
    return out


async def upload(
    db: AsyncSession, store: DocumentStore, user_id: int, kind: str, filename: str | None,
    read: Callable[[int], Awaitable[bytes]],
) -> DocumentUpload:
    """Check, read (at most the size limit plus one byte) and store an upload, and queue reading it. An
    identical file already stored comes back as a duplicate, with its parse task if it is still being read."""
    if kind not in KINDS:
        raise Invalid(f"kind must be one of {sorted(KINDS)}")
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise Unsupported(f"Unsupported file type; allowed: {sorted(ALLOWED_EXTENSIONS)}")
    limit = settings.upload_max_bytes
    data = await read(limit + 1)
    if len(data) > limit:
        raise TooLarge(f"File too large ({limit // 2**20}MB max)")
    name = Path(filename or "upload").name
    file = {"data": data, "ext": ext, "content_type": CONTENT_TYPES[ext], "tags": {"source": "upload"}}
    meta, created = store.put(user_id, kind, name, text="", status="processing", **file)
    if meta.status == "error":  # the same file again: read it once more (it may have timed out)
        meta = store.set_status(user_id, meta.id, "processing")
    task_id = None
    if meta.status == "processing":  # new, or a duplicate still being read: its pending task (or a new one)
        task = await ledger.add(db, "parse_document", {"doc_id": meta.id}, user_id=user_id, dedupe=True)
        task_id = task.id
    return DocumentUpload(document=DocumentOut.of(meta, duplicate=not created), task_id=task_id)


async def parse(store: DocumentStore, user_id: int, doc_id: str) -> dict:
    """worker-priority's parse_document: read the stored original and record its text (ready), or mark the
    document unreadable (error). A document deleted meanwhile, or already read, is left alone."""
    from jobfinder.profile.resume_parse import extract_text_in_worker

    try:
        meta = store.get(user_id, doc_id)
        path = store.original_path(user_id, doc_id)
    except storage.NotFound:
        return {"doc_id": doc_id, "skipped": "deleted"}
    # An error `unfinished` recorded is read again: the task retried by an admin, or after a lost commit.
    if meta.status != "processing" and not (meta.status == "error" and meta.fetch_error == UNFINISHED["parse_document"]):
        return {"doc_id": doc_id, "skipped": meta.status}
    text, error = "", None
    try:
        if path is None:
            raise FileNotFoundError("no original file")
        text = await extract_text_in_worker(path.read_bytes(), path.suffix)
    except Exception as e:  # a corrupt or crafted file, or one too slow to read (TimeoutError)
        log.info("document %s of user %s unreadable: %s", doc_id, user_id, type(e).__name__)
        error = UNREADABLE
    try:
        meta = store.replace_text(
            user_id, doc_id, text, fetch_error=error, fetched=False, status="error" if error else "ready"
        )
    except (storage.NotFound, FileNotFoundError):  # deleted while we read it
        return {"doc_id": doc_id, "skipped": "deleted"}
    return {"doc_id": doc_id, "status": meta.status, "chars": meta.chars, "error": error}


def original(store: DocumentStore, user_id: int, doc_id: str) -> tuple[DocumentMeta, Path]:
    """The document and the path of the file as uploaded."""
    meta = get(store, user_id, doc_id)
    try:
        path = store.original_path(user_id, doc_id)
    except storage.NotFound:
        raise NotFound("Not found") from None
    if path is None:
        raise NotFound("This document has no original file")
    return meta, path


def text(store: DocumentStore, user_id: int, doc_id: str) -> str:
    try:
        return store.read_text(user_id, doc_id)
    except storage.NotFound:
        raise NotFound("Not found") from None


def set_tags(store: DocumentStore, user_id: int, doc_id: str, tags: dict[str, str]) -> DocumentMeta:
    """Replace the custom tags; automatic tags (kind, ext, source, ...) are kept."""
    if MATCH_TAG in tags:
        raise Invalid(f"Tag {MATCH_TAG!r} is reserved")
    try:
        return store.set_tags(user_id, doc_id, tags)
    except storage.NotFound:
        raise NotFound("Not found") from None
    except StoreError as e:
        raise Invalid(str(e)) from None


def delete(store: DocumentStore, user_id: int, doc_id: str) -> None:
    try:
        store.delete(user_id, doc_id)
    except storage.NotFound:
        raise NotFound("Not found") from None
