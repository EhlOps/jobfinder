"""Small DB factories shared by the notification/question tests."""
from datetime import UTC, datetime

from jobfinder.ai.schemas import Consolidated, ConsolidatedQuestion
from jobfinder.models import ClarifyingQuestion, Job, JobMatch, Profile, User

_n = 0


def _next() -> int:
    global _n
    _n += 1
    return _n


async def make_user(db, email=None, *, tz="UTC", hour=8, digest=True, question_emails=True, name="Jane Doe", version=1, status=None):
    u = User(
        email=email or f"user{_next()}@x.com", password_hash="x", timezone=tz, digest_hour=hour,
        digest_enabled=digest, question_emails_enabled=question_emails,
    )
    db.add(u)
    await db.flush()
    db.add(Profile(user_id=u.id, status={"target_roles": ["Backend Engineer"]} if status is None else status,
                   background={"name": name} if name else {}, version=version))
    await db.commit()
    return u


async def make_match(db, user_id, *, title=None, company="Acme", score=80, confidence=0.9, status="new", active=True,
                     unknowns=None, collected=False, digested=False, version=1, url=None):
    n = _next()
    job = Job(
        source="greenhouse", external_id=f"f{n}", company_name=company, title=title or f"Backend Engineer {n}",
        url=url or f"https://jobs.example/{n}", location="Boston, MA", workplace_type="hybrid", description_text="desc",
        is_active=active, dedupe_hash=f"fh{n}", salary_min=120000, salary_max=160000,
    )
    db.add(job)
    await db.flush()
    m = JobMatch(
        user_id=user_id, job_id=job.id, profile_version=version, prefilter_score=50, llm_score=score, confidence=confidence,
        verdict="strong" if score >= 80 else "good" if score >= 65 else "stretch" if score >= 45 else "no",
        reasons=["Reason one", "Reason two", "Reason three"], gaps=["g"], unknowns=unknowns or [], status=status,
        questions_collected=collected, digested_at=datetime.now(UTC) if digested else None,
    )
    db.add(m)
    await db.commit()
    return m, job


async def make_question(db, user_id, text="Have you used Kafka?", *, status="pending", match_ids=None):
    q = ClarifyingQuestion(user_id=user_id, question=text, why="job needs it", match_ids=match_ids or [], status=status)
    db.add(q)
    await db.commit()
    return q


def unknown(q, why="the job asks for it"):
    return {"question": q, "why": why}


class FakeAI:
    """consolidate_questions: by default every raw question becomes its own question."""

    def __init__(self, result: Consolidated | None = None, error: Exception | None = None):
        self.result, self.error, self.calls = result, error, []

    async def consolidate_questions(self, facts, asked, raw, max_questions=6):
        self.calls.append({"facts": facts, "asked": asked, "raw": raw, "max": max_questions})
        if self.error:
            raise self.error
        return self.result or Consolidated(
            questions=[ConsolidatedQuestion(question=q, why=w, sources=[i]) for i, (q, w) in enumerate(raw)]
        )
