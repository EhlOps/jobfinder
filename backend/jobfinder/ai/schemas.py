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


class RequirementCheck(BaseModel):
    requirement: str  # one concrete requirement from the posting, e.g. "3+ years of Python"
    importance: Literal["must", "nice"] = "must"
    status: Literal["met", "partial", "unknown", "unmet"]
    evidence: str = ""  # what in the profile supports the status (empty when there is none)
    question: str = ""  # direct question to the candidate; set for partial/unknown (and unmet if worth asking)


class MatchScore(BaseModel):
    score: int = Field(default=0, ge=0, le=100)  # the model's own gut number; the stored score is computed in code
    confidence: float = Field(default=0.5, ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)  # why it fits (or doesn't), grounded in the profile
    gaps: list[str] = Field(default_factory=list)  # requirements the profile doesn't cover
    unknowns: list[Unknown] = Field(default_factory=list)  # things only the candidate can answer
    requirements: list[RequirementCheck] = Field(default_factory=list, max_length=14)
    preference_fit: int = Field(default=70, ge=0, le=100)  # level/location/pay/company fit with stated preferences
    hire_verdict: Literal["yes", "maybe", "no"] = "maybe"  # would a recruiter put this candidate forward?
    recruiter_take: str = ""  # one line, as a recruiter would say it


def verdict_for(score: int) -> str:
    return "strong" if score >= 80 else "good" if score >= 65 else "stretch" if score >= 45 else "no"


class ConsolidatedQuestion(BaseModel):
    question: str
    why: str = ""
    sources: list[int] = Field(default_factory=list)  # indexes of the input questions this one replaces


class Consolidated(BaseModel):
    questions: list[ConsolidatedQuestion] = Field(default_factory=list)
    already_answered: list[int] = Field(default_factory=list)  # input indexes the candidate has effectively answered


class DossierItem(BaseModel):
    label: str  # e.g. "Python", "Ownership", "Work authorization"
    detail: str  # what a recruiter would write down
    confidence: float = Field(default=0.5, ge=0, le=1)


class AuditQuestion(BaseModel):
    question: str
    why: str = ""
    dimension: Literal["skills", "impact", "scope", "logistics", "motivation"] = "skills"


class Dossier(BaseModel):
    headline: str = ""  # one line: who this person is as a candidate
    years_experience: str = ""
    skills: list[DossierItem] = Field(default_factory=list)  # depth per core skill
    experience: list[DossierItem] = Field(default_factory=list)  # scope, ownership, quantified impact
    logistics: list[DossierItem] = Field(default_factory=list)  # authorization, location, availability, comp
    strengths: list[str] = Field(default_factory=list)  # strongest evidence, reusable in cover letters
    concerns: list[str] = Field(default_factory=list)  # what would make a recruiter hesitate
    hire_view: str = ""  # "I could hire this person for X because..." / "I could not yet, because..."


class ProfileAudit(BaseModel):
    dossier: Dossier
    readiness: int = Field(ge=0, le=100)
    dimensions: dict[str, int] = Field(default_factory=dict)  # skills|impact|scope|logistics|motivation -> 0-100
    questions: list[AuditQuestion] = Field(default_factory=list, max_length=8)


class ResumeContact(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    links: list[str] = Field(default_factory=list)


class TailoredResume(BaseModel):
    """A resume rewritten for one posting. Sections are required so a response cannot silently drop one."""
    contact: ResumeContact
    summary: str
    skills: list[str]  # ordered most relevant to the posting first
    experience: list[Experience]
    projects: list[Project]
    education: list[Education]
