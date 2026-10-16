"""The user's own links (GitHub, portfolio, LinkedIn, ...), stored as documents of kind "link"."""
from typing import Literal

from pydantic import BaseModel, HttpUrl

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

    @classmethod
    def of(cls, m: DocumentMeta) -> "ProfileLinkOut":
        return cls(
            id=m.id,
            kind=m.tags.get("link_kind", "other"),
            url=m.tags.get("url", ""),
            fetched=m.chars > 0 and m.fetch_error is None,
            fetch_error=m.fetch_error,
        )
