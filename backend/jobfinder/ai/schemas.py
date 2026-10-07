"""Pydantic models the AI functions return. Their JSON schema is passed to `claude --json-schema`."""
from typing import Literal

from pydantic import BaseModel, Field


class Education(BaseModel):
    school: str
    degree: str = ""
    field: str = ""
    start: str = ""
    end: str = ""
    gpa: str = ""


class Experience(BaseModel):
    kind: Literal["internship", "co-op", "full-time", "part-time", "research", "other"] = "other"
    company: str
    title: str = ""
    start: str = ""
    end: str = ""
    location: str = ""
    summary: str = ""
    bullets: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class Project(BaseModel):
    name: str
    description: str = ""
    technologies: list[str] = Field(default_factory=list)
    url: str = ""


class Skill(BaseModel):
    name: str
    level: str = ""  # beginner | intermediate | advanced | expert, or empty if unclear


class Background(BaseModel):
    name: str = ""  # full name as written on the resume (used to sign cover letters)
    summary: str = ""
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)


class FollowupQuestion(BaseModel):
    question: str
    why: str = ""  # shown to the user: what gap this fills


class FollowupQuestions(BaseModel):
    questions: list[FollowupQuestion]


class Ping(BaseModel):
    ok: bool


class Unknown(BaseModel):
    question: str  # a question for the candidate, e.g. "Have you used Kafka in production?"
    why: str = ""


class MatchScore(BaseModel):
    score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)  # why it fits (or doesn't), grounded in the profile
    gaps: list[str] = Field(default_factory=list)  # requirements the profile doesn't cover
    unknowns: list[Unknown] = Field(default_factory=list)  # things only the candidate can answer


def verdict_for(score: int) -> str:
    return "strong" if score >= 80 else "good" if score >= 65 else "stretch" if score >= 45 else "no"


class ConsolidatedQuestion(BaseModel):
    question: str
    why: str = ""
    sources: list[int] = Field(default_factory=list)  # indexes of the input questions this one replaces


class Consolidated(BaseModel):
    questions: list[ConsolidatedQuestion] = Field(default_factory=list)
    already_answered: list[int] = Field(default_factory=list)  # input indexes the candidate has effectively answered
