"""A match's tailored resume as the API shows and accepts it, with its keyword coverage.

TailoredResumeOut has the same fields as the AI's claude.schemas.TailoredResume but is defined here, so a change to
the prompt's schema cannot silently change the API. The router reads the AI-written file straight into it, so
the two must stay field-for-field identical (tests/test_architecture.py checks).
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ResumeContact(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: list[str] = Field(default_factory=list)


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


class TailoredResumeOut(BaseModel):
    """A resume rewritten for one posting. Sections are required so a response cannot silently drop one."""
    contact: ResumeContact
    summary: str
    skills: list[str]  # ordered most relevant to the posting first
    experience: list[Experience]
    projects: list[Project]
    education: list[Education]


class CoverageOut(BaseModel):
    percent: int
    covered: list[str]
    missing: list[str]
    loose: list[str]  # present only in another spelling; missing for literal-matching ATSes
    ats: str
    notes: list[str]


class ResumeOut(BaseModel):
    content: TailoredResumeOut
    edited: bool
    coverage: CoverageOut  # computed from the saved text, so it follows every edit
    generated_at: datetime
    updated_at: datetime


class ResumeState(BaseModel):
    resume: ResumeOut | None
    pending_task_id: int | None  # a tailoring run is in progress right now


class ResumeGenerateIn(BaseModel):
    force: bool = False  # required to overwrite a resume the user has edited


class EditIn(BaseModel):
    content: TailoredResumeOut = Field(description="The complete edited resume")
