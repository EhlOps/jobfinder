"""An uploaded document (resume, portfolio, ...) as the API shows it."""
from typing import Literal, cast

from pydantic import BaseModel

from jobfinder.storage.documents import DocumentMeta

# processing while worker-io reads the file (or fetches the link), then ready or error (#51)
DocumentStatus = Literal["processing", "ready", "error"]


class DocumentOut(BaseModel):
    id: str
    kind: str
    filename: str
    size: int
    chars: int
    tags: dict[str, str]
    created_at: str
    duplicate: bool = False
    status: DocumentStatus

    @classmethod
    def of(cls, m: DocumentMeta, duplicate: bool = False) -> "DocumentOut":
        return cls(
            id=m.id, kind=m.kind, filename=m.filename, size=m.size, chars=m.chars,
            tags=m.tags, created_at=m.created_at, duplicate=duplicate, status=cast(DocumentStatus, m.status),
        )


class DocumentUpload(BaseModel):
    """An accepted upload: the document, and the parse_document task reading it while it is processing.
    Poll GET /api/tasks/{task_id}, then GET the document. task_id is null when there is nothing left to
    wait for (SYNC_UPLOADS, or a duplicate of a file already read)."""
    document: DocumentOut
    task_id: int | None
