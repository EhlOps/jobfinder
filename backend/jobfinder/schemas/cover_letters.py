"""A match's cover letter: the draft, its state, and the bodies for drafting and editing it."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Tone = Literal["professional", "warm", "concise", "enthusiastic"]
MAX_LETTER_CHARS = 10_000


class LetterOut(BaseModel):
    content: str
    tone: str
    edited: bool
    words: int
    stale: bool  # drafted before the latest profile update
    generated_at: datetime
    updated_at: datetime


class LetterState(BaseModel):
    letter: LetterOut | None
    pending_task_id: int | None  # a draft is being written right now


class LetterGenerateIn(BaseModel):
    tone: Tone = "professional"
    notes: str = Field(default="", max_length=500)
    force: bool = False  # required to overwrite a letter the user has edited


class ContentIn(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_LETTER_CHARS)
