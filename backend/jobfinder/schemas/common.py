"""Pieces several routers share: the reference to a queued task, and the http(s)-only URL field."""
from typing import Annotated

from pydantic import AfterValidator, BaseModel

from jobfinder.core.urls import safe_http_url

# A link from third-party job data: anything but an absolute http(s) URL becomes "", so a javascript: or
# data: link never reaches an href. Also covers rows stored before ingestion started checking.
HttpUrlStr = Annotated[str, AfterValidator(safe_http_url)]


class TaskRef(BaseModel):
    task_id: int
