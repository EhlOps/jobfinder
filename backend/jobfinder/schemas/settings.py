"""The user's email and match-budget settings, and the result of sending a preview of the daily email."""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator


class SettingsBody(BaseModel):
    digest_enabled: bool
    digest_hour: int = Field(ge=0, le=23)
    timezone: str = Field(min_length=1, max_length=64)
    question_emails_enabled: bool
    match_budget_enabled: bool = True
    match_budget: int | None = Field(default=None, ge=1, le=1000)  # None = keep the current value

    @field_validator("timezone")
    @classmethod
    def _valid_zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown timezone") from None
        return v


class PreviewResult(BaseModel):
    to: str
    matches: int
    questions: int
