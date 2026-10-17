"""Pieces several routers share: the reference to a queued task, the http(s)-only URL field, and file downloads."""
from typing import Annotated
from urllib.parse import quote

from fastapi.responses import Response
from pydantic import AfterValidator, BaseModel

from jobfinder.core.urls import safe_http_url

# A link from third-party job data: anything but an absolute http(s) URL becomes "", so a javascript: or
# data: link never reaches an href. Also covers rows stored before ingestion started checking.
HttpUrlStr = Annotated[str, AfterValidator(safe_http_url)]


class TaskRef(BaseModel):
    task_id: int


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF = "application/pdf"


def file_download(data: bytes, filename: str, media_type: str = DOCX) -> Response:
    """A file the browser saves as `filename` (percent-encoded per RFC 5987, so any name fits the header)."""
    return Response(
        data, media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"},
    )
