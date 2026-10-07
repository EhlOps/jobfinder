from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from jobfinder.auth.security import current_user
from jobfinder.models import User
from jobfinder.profile.resume_parse import ALLOWED_EXTENSIONS, ParserBusy, extract_text_async
from jobfinder.storage.documents import DocumentMeta, DocumentStore, NotFound, StoreError, get_store

router = APIRouter(prefix="/api/documents", tags=["documents"])

MAX_BYTES = 10 * 1024 * 1024
KINDS = {"resume", "portfolio", "other"}
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}

Store = Annotated[DocumentStore, Depends(get_store)]
CurrentUser = Annotated[User, Depends(current_user)]


class DocumentOut(BaseModel):
    id: str
    kind: str
    filename: str
    size: int
    chars: int
    tags: dict[str, str]
    created_at: str
    duplicate: bool = False

    @classmethod
    def of(cls, m: DocumentMeta, duplicate: bool = False) -> "DocumentOut":
        return cls(
            id=m.id, kind=m.kind, filename=m.filename, size=m.size, chars=m.chars,
            tags=m.tags, created_at=m.created_at, duplicate=duplicate,
        )


def _not_found(e: StoreError) -> HTTPException:
    return HTTPException(404, "Not found")


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    user: CurrentUser,
    store: Store,
    kind: str | None = None,
    tag: Annotated[list[str], Query(description="key:value, repeatable")] = [],  # noqa: B006
):
    wanted = {}
    for t in tag:
        key, sep, value = t.partition(":")
        if not sep:
            raise HTTPException(422, "tag must look like key:value")
        wanted[key] = value
    docs = store.list(user.id, kind=kind, tags=wanted)
    if kind is None:
        docs = [d for d in docs if d.kind != "link"]  # links have their own endpoint
    return [DocumentOut.of(d) for d in docs]


@router.post("", response_model=DocumentOut, status_code=201)
async def upload_document(
    user: CurrentUser,
    store: Store,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Form()] = "resume",
):
    if kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(KINDS)}")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, f"Unsupported file type; allowed: {sorted(ALLOWED_EXTENSIONS)}")
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large (10MB max)")
    try:
        text = await extract_text_async(data, ext)
    except ParserBusy:
        raise HTTPException(503, "Busy reading other files, try again in a minute") from None
    except Exception:
        raise HTTPException(422, "Could not read that file") from None

    meta, created = store.put(
        user.id, kind, Path(file.filename or "upload").name,
        data=data, text=text, ext=ext, content_type=CONTENT_TYPES[ext], tags={"source": "upload"},
    )
    return DocumentOut.of(meta, duplicate=not created)


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_document(doc_id: str, user: CurrentUser, store: Store):
    try:
        return DocumentOut.of(store.get(user.id, doc_id))
    except NotFound as e:
        raise _not_found(e) from None


@router.get("/{doc_id}/download")
async def download_document(doc_id: str, user: CurrentUser, store: Store):
    try:
        meta = store.get(user.id, doc_id)
        path = store.original_path(user.id, doc_id)
    except NotFound as e:
        raise _not_found(e) from None
    if path is None:
        raise HTTPException(404, "This document has no original file")
    return FileResponse(path, media_type=meta.content_type, filename=meta.filename)


@router.get("/{doc_id}/text", response_class=PlainTextResponse)
async def document_text(doc_id: str, user: CurrentUser, store: Store):
    try:
        return store.read_text(user.id, doc_id)
    except NotFound as e:
        raise _not_found(e) from None


@router.put("/{doc_id}/tags", response_model=DocumentOut)
async def set_tags(doc_id: str, tags: dict[str, str], user: CurrentUser, store: Store):
    """Replace this document's custom tags. Automatic tags (kind, ext, source, ...) are kept."""
    try:
        return DocumentOut.of(store.set_tags(user.id, doc_id, tags))
    except NotFound as e:
        raise _not_found(e) from None
    except StoreError as e:
        raise HTTPException(422, str(e)) from None


@router.delete("/{doc_id}", status_code=204)
async def delete_document(doc_id: str, user: CurrentUser, store: Store):
    try:
        store.delete(user.id, doc_id)
    except NotFound as e:
        raise _not_found(e) from None
