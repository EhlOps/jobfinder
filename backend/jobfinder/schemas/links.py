"""The user's own links (GitHub, portfolio, LinkedIn, ...), stored as documents of kind "link"."""
from typing import Literal, cast

from pydantic import BaseModel, HttpUrl

from jobfinder.schemas.documents import DocumentStatus
from jobfinder.storage.documents import DocumentMeta


class ProfileLinkIn(BaseModel):
    kind: Literal["github", "portfolio", "linkedin", "x", "other"]
    url: HttpUrl


class ProfileLinkOut(BaseModel):
    id: str
    kind: str
    url: str
    fetched: bool
    fetch_error: str | None
    status: DocumentStatus

    @classmethod
    def of(cls, m: DocumentMeta) -> "ProfileLinkOut":
        return cls(
            id=m.id,
            kind=m.tags.get("link_kind", "other"),
            url=m.tags.get("url", ""),
            fetched=m.chars > 0 and m.fetch_error is None,
            fetch_error=m.fetch_error,
            status=cast(DocumentStatus, m.status),
        )


class LinkUpload(BaseModel):
    """An added or refreshed link, and the fetch_link task fetching it while it is processing. task_id is
    null when nothing is fetched: LinkedIn and X links are only saved."""
    link: ProfileLinkOut
    task_id: int | None
