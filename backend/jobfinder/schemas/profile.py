"""The profile: the job-search status a user fills in, the profile as returned, and the facts they've added."""
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

WorkAuth = Literal["citizen", "permanent_resident", "f1_opt", "stem_opt", "h1b_transfer", "other"]
# Whether each authorization means the person will need an employer to sponsor them (None = can't tell)
SPONSORSHIP_NEEDED: dict[str, bool | None] = {
    "citizen": False, "permanent_resident": False, "f1_opt": True, "stem_opt": True, "h1b_transfer": True, "other": None,
}


def derive_needs_sponsorship(work_authorization: str | None, explicit: bool | None = None) -> bool | None:
    """An explicit answer wins; otherwise it follows from the work authorization."""
    return explicit if explicit is not None else SPONSORSHIP_NEEDED.get(work_authorization or "")


class Status(BaseModel):
    """Where the person is in their job search and what they want."""

    is_new_grad: bool | None = None
    graduation_date: str | None = None  # YYYY-MM
    need_job_by: str | None = None  # YYYY-MM-DD
    target_roles: list[str] = []
    seniority: list[str] = []  # intern|new_grad|junior|mid|senior|staff
    target_locations: list[str] = []
    remote_preference: str | None = None  # remote|hybrid|onsite|any
    willing_to_relocate: bool | None = None
    needs_visa_sponsorship: bool | None = None
    work_authorization: WorkAuth | None = None
    salary_min: int | None = None
    salary_target: int | None = None
    prestige_preference: int | None = Field(default=None, ge=1, le=5)  # 1 = don't care, 5 = top-tier only
    company_sizes: list[str] = []  # startup|mid|large
    industries: list[str] = []
    notes: str | None = None

    @model_validator(mode="after")
    def _derive_sponsorship(self):
        self.needs_visa_sponsorship = derive_needs_sponsorship(self.work_authorization, self.needs_visa_sponsorship)
        return self


class ProfileOut(BaseModel):
    status: dict[str, Any]
    background: dict[str, Any]
    version: int
    career_stage: str | None = None  # derived from the status (services/profile.py `profile_out`)


class FactIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=5000)
    source: str = "onboarding"


class FactOut(FactIn):
    id: int

    model_config = {"from_attributes": True}
