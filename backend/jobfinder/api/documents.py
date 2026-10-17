"""Uploaded documents: list, upload, read back, retag and delete the user's files."""
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from jobfinder.api.deps import CurrentUser, Store
from jobfinder.schemas.documents import DocumentOut
from jobfinder.services import documents as service

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    user: CurrentUser,
    store: Store,
    kind: str | None = None,
    tag: Annotated[list[str], Query(description="key:value, repeatable")] = [],  # noqa: B006
):
    return [DocumentOut.of(d) for d in service.list_documents(store, user.id, kind, tag)]


@router.post("", response_model=DocumentOut, status_code=201)
async def upload_document(
    user: CurrentUser,
    store: Store,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Form()] = "resume",
):
    meta, created = await service.upload(store, user.id, kind, file.filename, file.read)
    return DocumentOut.of(meta, duplicate=not created)


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_document(doc_id: str, user: CurrentUser, store: Store):
    return DocumentOut.of(service.get(store, user.id, doc_id))


@router.get("/{doc_id}/download")
async def download_document(doc_id: str, user: CurrentUser, store: Store):
    meta, path = service.original(store, user.id, doc_id)
    return FileResponse(path, media_type=meta.content_type, filename=meta.filename)


@router.get("/{doc_id}/text", response_class=PlainTextResponse)
async def document_text(doc_id: str, user: CurrentUser, store: Store):
    return service.text(store, user.id, doc_id)


@router.put("/{doc_id}/tags", response_model=DocumentOut)
async def set_tags(doc_id: str, tags: dict[str, str], user: CurrentUser, store: Store):
    """Replace this document's custom tags. Automatic tags (kind, ext, source, ...) are kept."""
    return DocumentOut.of(service.set_tags(store, user.id, doc_id, tags))


@router.delete("/{doc_id}", status_code=204)
async def delete_document(doc_id: str, user: CurrentUser, store: Store):
    service.delete(store, user.id, doc_id)
