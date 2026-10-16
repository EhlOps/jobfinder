"""A background task as its owner polls it."""
from typing import Any

from pydantic import BaseModel


class TaskOut(BaseModel):
    id: int
    kind: str
    status: str
    result: dict[str, Any] | None
    error: str | None

    model_config = {"from_attributes": True}
