"""Clarifying questions: the open ones with the jobs that raised them, and the answers to them."""
from pydantic import BaseModel, Field

from jobfinder.questions.service import MAX_ANSWER_CHARS


class QuestionJob(BaseModel):
    match_id: int
    title: str
    company: str


class QuestionOut(BaseModel):
    id: int
    question: str
    why: str
    jobs: list[QuestionJob]


class QuestionList(BaseModel):
    questions: list[QuestionOut]


class QuestionAnswerIn(BaseModel):
    id: int
    answer: str = Field(default="", max_length=MAX_ANSWER_CHARS)
    skip: bool = False


class QuestionAnswersIn(BaseModel):
    answers: list[QuestionAnswerIn] = Field(max_length=30)


class AnswerResult(BaseModel):
    answered: int
    skipped: int
    remaining: int
