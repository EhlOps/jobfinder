"""'Strengthen my profile': the audit as the UI shows it, and the answers to its questions."""
from typing import Any

from pydantic import BaseModel, Field

from jobfinder.profile.interview import MAX_ANSWER_CHARS


class InterviewOut(BaseModel):
    readiness: int
    dimensions: dict[str, int]
    dossier: dict[str, Any]
    questions: list[dict[str, Any]]
    audited: bool
    stale: bool  # the profile changed since the last audit


class InterviewAnswerIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(default="", max_length=MAX_ANSWER_CHARS)
    skip: bool = False


class InterviewAnswersIn(BaseModel):
    answers: list[InterviewAnswerIn] = Field(max_length=20)
