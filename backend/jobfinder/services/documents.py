"""Uploaded documents: what may be uploaded, reading its text, and the user's files in the document store.

Store-only, no database: the store's NotFound becomes the API's 404, and every failure keeps the
status and wording the routers always answered with.
"""
from collections.abc import Awaitable, Callable
from pathlib import Path

from jobfinder.core.config import settings
from jobfinder.core.errors import Invalid, NotFound, TooLarge, Unavailable, Unsupported
from jobfinder.profile.resume_parse import ALLOWED_EXTENSIONS, ParserBusy, extract_text_async
from jobfinder.storage import documents as storage
from jobfinder.storage.documents import (
    MATCH_TAG,
    DocumentMeta,
    DocumentStore,
    StoreError,
    is_tailored,
)

KINDS = {"resume", "portfolio", "other"}
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}


def get(store: DocumentStore, user_id: int, doc_id: str) -> DocumentMeta:
    try:
        return store.get(user_id, doc_id)
    except storage.NotFound:
        raise NotFound("Not found") from None


def list_documents(store: DocumentStore, user_id: int, kind: str | None, tags: list[str]) -> list[DocumentMeta]:
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
    return docs


async def upload(
    store: DocumentStore, user_id: int, kind: str, filename: str | None, read: Callable[[int], Awaitable[bytes]]
) -> tuple[DocumentMeta, bool]:
    """Check, read (at most the size limit plus one byte) and store an upload. Returns (meta, created):
    an identical file already stored comes back with created=False."""
    if kind not in KINDS:
        raise Invalid(f"kind must be one of {sorted(KINDS)}")
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise Unsupported(f"Unsupported file type; allowed: {sorted(ALLOWED_EXTENSIONS)}")
    limit = settings.upload_max_bytes
    data = await read(limit + 1)
    if len(data) > limit:
        raise TooLarge(f"File too large ({limit // 2**20}MB max)")
    try:
        text = await extract_text_async(data, ext)
    except ParserBusy:
        raise Unavailable("Busy reading other files, try again in a minute") from None
    except Exception:
        raise Invalid("Could not read that file") from None
    return store.put(
        user_id, kind, Path(filename or "upload").name,
        data=data, text=text, ext=ext, content_type=CONTENT_TYPES[ext], tags={"source": "upload"},
    )


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
