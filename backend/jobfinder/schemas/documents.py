"""An uploaded document (resume, portfolio, ...) as the API shows it."""
from pydantic import BaseModel

from jobfinder.storage.documents import DocumentMeta


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
