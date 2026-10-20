"""The daily email: new matches (if the digest is on) and clarifying questions (if question emails are on),
sent once per day around each user's chosen hour in their own timezone. Never sends an empty email."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.core.config import settings
from jobfinder.ingest.base import safe_http_url
from jobfinder.integrations import smtp as mail
from jobfinder.models import ClarifyingQuestion, Job, JobMatch, Profile, User
from jobfinder.notify.tokens import make_token
from jobfinder.repositories.questions import OPEN

log = logging.getLogger("jobfinder.daily")
VERDICT_LABEL = {"strong": "Strong fit", "good": "Good fit", "stretch": "Stretch", "no": "Poor fit"}

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    # Escape the HTML part only: "j2" alone would also match daily.txt.j2 and corrupt "&" in the plain text.
    autoescape=select_autoescape(enabled_extensions=("html.j2",), disabled_extensions=("txt.j2",), default_for_string=True),
    trim_blocks=True, lstrip_blocks=True,
)


@dataclass
class Content:
    matches: list[JobMatch]
    jobs: dict[int, Job]
    more_count: int
    questions: list[ClarifyingQuestion]


def user_zone(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _at_hour(local: datetime, hour: int, *, days: int = 0) -> datetime:
    """`hour` o'clock local time, `days` after local's date (wall-clock arithmetic, same zone)."""
    day = local.replace(tzinfo=None) + timedelta(days=days)
    return day.replace(hour=hour, minute=0, second=0, microsecond=0, tzinfo=local.tzinfo)


def is_due(user: User, now: datetime) -> bool:
    """True from the user's chosen local hour for a few hours, once per local day."""
    if user.digest_hour is None:
        return False
    local = now.astimezone(user_zone(user))
    # The most recent local digest_hour o'clock; before today's, that was yesterday's (so the window can cross midnight).
    scheduled = _at_hour(local, user.digest_hour)
    if local < scheduled:
        scheduled = _at_hour(local, user.digest_hour, days=-1)
    if not timedelta(0) <= local - scheduled < timedelta(hours=settings.digest_send_window_hours):
        return False
    return not (user.last_digest_at and user.last_digest_at >= scheduled)


def questions_due(user: User, now: datetime) -> datetime | None:
    """The user's next digest time (UTC) when it is at most QUESTIONS_LEAD_HOURS away, else None: question
    collection runs in that window so the digest can send what it collected (#50). A lead of 0 never is."""
    if user.digest_hour is None:
        return None
    local = now.astimezone(user_zone(user))
    upcoming = _at_hour(local, user.digest_hour)
    if upcoming <= local:
        upcoming = _at_hour(local, user.digest_hour, days=1)
    upcoming = upcoming.astimezone(UTC)  # so a DST change in between counts real hours
    return upcoming if upcoming - now <= timedelta(hours=settings.questions_lead_hours) else None


async def build_content(db: AsyncSession, user: User, *, preview: bool = False) -> Content:
    s = settings
    matches: list[JobMatch] = []
    jobs: dict[int, Job] = {}
    more = 0
    if user.digest_enabled or preview:
        q = (
            sa.select(JobMatch, Job)
            .join(Job, Job.id == JobMatch.job_id)
            .where(
                JobMatch.user_id == user.id, JobMatch.status == "new", Job.is_active.is_(True),
                JobMatch.llm_score >= (50 if preview else s.digest_min_score),
            )
            .order_by(JobMatch.llm_score.desc(), JobMatch.id.desc())
        )
        if not preview:
            q = q.where(JobMatch.digested_at.is_(None))
        rows = (await db.execute(q)).all()
        top = rows[: s.digest_max_matches]
        more = max(0, len(rows) - len(top))
        matches, jobs = [m for m, _ in top], {m.id: j for m, j in top}
    questions: list[ClarifyingQuestion] = []
    if user.question_emails_enabled or preview:
        qq = sa.select(ClarifyingQuestion).where(ClarifyingQuestion.user_id == user.id)
        qq = qq.where(ClarifyingQuestion.status.in_(OPEN if preview else ("pending",)))
        questions = list((await db.scalars(qq.order_by(ClarifyingQuestion.id).limit(s.questions_per_email))).all())
    return Content(matches, jobs, more, questions)


def _details(job: Job) -> str:
    bits = [job.location[:60] if job.location else "", (job.workplace_type or "").capitalize()]
    if job.salary_min or job.salary_max:
        lo, hi = job.salary_min, job.salary_max
        bits.append(f"${round((lo or hi) / 1000)}k-{round(hi / 1000)}k" if lo and hi and lo != hi else f"${round((lo or hi) / 1000)}k")
    return " · ".join(b for b in bits if b)


def render(user: User, content: Content, *, greeting_name: str = "", preview: bool = False) -> tuple[str, str, str, str]:
    """Returns (subject, text, html, unsubscribe_url)."""
    base = settings.public_base_url.rstrip("/")
    unsub = f"{base}/api/unsubscribe/{make_token('unsubscribe', user.id)}"
    ctx = {
        "greeting": f"Hi {greeting_name}," if greeting_name else "Hi,",
        "preview": preview,
        "matches": [
            {
                "title": content.jobs[m.id].title, "company": content.jobs[m.id].company_name, "score": m.llm_score,
                "verdict": VERDICT_LABEL.get(m.verdict, m.verdict), "details": _details(content.jobs[m.id]),
                "reasons": (m.reasons or [])[:2], "view_url": f"{base}/matches/{m.id}", "apply_url": safe_http_url(content.jobs[m.id].url),
            }
            for m in content.matches
        ],
        "more_count": content.more_count,
        "questions": [{"question": q.question} for q in content.questions],
        "questions_url": f"{base}/q/{make_token('questions', user.id)}",
        "matches_url": f"{base}/", "settings_url": f"{base}/settings", "unsubscribe_url": unsub,
    }
    n, q = len(content.matches), len(content.questions)
    if n and q:
        subject = f"{n} new job match{'es' if n != 1 else ''} + {q} quick question{'s' if q != 1 else ''}"
    elif n:
        top = content.matches[0]
        subject = f"{n} new job match{'es' if n != 1 else ''} - top pick: {content.jobs[top.id].company_name}, {content.jobs[top.id].title}"
    else:
        subject = f"{q} quick question{'s' if q != 1 else ''} to sharpen your job matches"
    if preview:
        subject = f"[Preview] {subject}"
    text = _env.get_template("daily.txt.j2").render(**ctx)
    html = _env.get_template("daily.html.j2").render(**ctx)
    return subject[:200], text, html, unsub


async def _claim(db: AsyncSession, user: User, content: Content, now: datetime) -> bool:
    """Mark everything in this email as sent *before* the SMTP call, so a crash or a concurrent run can't
    send it twice (at-most-once: a crash between claim and send loses that day's email rather than repeating it).
    The user row is a compare-and-set on last_digest_at, so only one runner wins."""
    prev = user.last_digest_at
    won = await db.execute(
        sa.update(User).where(User.id == user.id, User.last_digest_at.is_(None) if prev is None else User.last_digest_at == prev)
        .values(last_digest_at=now)
    )
    if not won.rowcount:
        await db.rollback()
        return False
    if content.matches:
        await db.execute(sa.update(JobMatch).where(JobMatch.id.in_([m.id for m in content.matches])).values(digested_at=now))
    if content.questions:
        await db.execute(
            sa.update(ClarifyingQuestion).where(ClarifyingQuestion.id.in_([q.id for q in content.questions]))
            .values(status="emailed", emailed_at=now)
        )
    await db.commit()
    return True


async def _release(db: AsyncSession, user_id: int, content: Content, prev: datetime | None) -> None:
    """The send failed: give the matches/questions back so the next tick tries again."""
    await db.rollback()
    if content.matches:
        await db.execute(sa.update(JobMatch).where(JobMatch.id.in_([m.id for m in content.matches])).values(digested_at=None))
    if content.questions:
        await db.execute(
            sa.update(ClarifyingQuestion).where(ClarifyingQuestion.id.in_([q.id for q in content.questions]))
            .values(status="pending", emailed_at=None)
        )
    await db.execute(sa.update(User).where(User.id == user_id).values(last_digest_at=prev))
    await db.commit()


async def send_daily(db: AsyncSession, user: User, *, now: datetime | None = None, preview: bool = False) -> dict | None:
    """Build and send one user's email. Returns a summary, or None when there is nothing to say (or another
    run already sent it). Real sends claim their matches/questions first, and release them if SMTP fails."""
    now = now or datetime.now(UTC)
    content = await build_content(db, user, preview=preview)
    if not content.matches and not content.questions:
        return None
    profile = await db.get(Profile, user.id)
    full_name = ((profile.background or {}).get("name") or "") if profile else ""
    subject, text, html, unsub = render(user, content, greeting_name=full_name.split(" ")[0] if full_name else "", preview=preview)
    prev, user_id = user.last_digest_at, user.id
    if not preview and not await _claim(db, user, content, now):
        return None
    try:
        await mail.send_email(
            user.email, subject, text, html,
            headers={"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
        )
    except BaseException:
        if not preview:
            await _release(db, user_id, content, prev)
        raise
    if not preview:
        await db.refresh(user)
    return {"matches": len(content.matches), "questions": len(content.questions), "subject": subject}


async def _email_users(db: AsyncSession, *where) -> list[User]:
    """Active users with a chosen hour, narrowed by `where`."""
    return list((await db.scalars(sa.select(User).where(
        User.password_hash.is_not(None), User.digest_hour.is_not(None), *where,
    ))).all())


async def due_users(db: AsyncSession, now: datetime) -> list[User]:
    users = await _email_users(db, sa.or_(User.digest_enabled.is_(True), User.question_emails_enabled.is_(True)))
    return [u for u in users if is_due(u, now)]


async def question_users(db: AsyncSession, now: datetime) -> list[tuple[User, datetime]]:
    """Users with question emails on, matches still to review and a digest coming up, each with that
    digest's time (questions_due)."""
    unreviewed = sa.exists().where(
        JobMatch.user_id == User.id, JobMatch.questions_collected.is_(False),
        JobMatch.status.in_(("new", "saved")), JobMatch.job_id == Job.id, Job.is_active.is_(True),
    )
    users = await _email_users(db, User.question_emails_enabled.is_(True), unreviewed)
    return [(u, at) for u in users if (at := questions_due(u, now))]
