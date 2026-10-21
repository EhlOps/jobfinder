import asyncio
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from factories import make_match, make_question, make_user, unknown

from jobfinder.config import get_settings
from jobfinder.models import ClarifyingQuestion, JobMatch, Task, User
from jobfinder.notify import daily
from jobfinder.notify import email as mail
from jobfinder.notify.email import EmailError
from jobfinder.notify.tokens import read_token
from jobfinder.tasks import ledger
from tests.fakes import FakeQuestionsAI, run_task


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    async def fake_send(to, subject, text, html=None, headers=None):
        sent.append({"to": to, "subject": subject, "text": text, "html": html, "headers": headers or {}})

    monkeypatch.setattr(mail, "send_email", fake_send)
    return sent


def at(hour, day=3, minute=5):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


# ── when is an email due ─────────────────────────────────────────────────
def user_obj(tz="UTC", hour=8, last=None):
    return User(email="u@x.com", password_hash="x", timezone=tz, digest_hour=hour, last_digest_at=last)


def test_due_window_in_utc():
    u = user_obj(hour=8)
    assert not daily.is_due(u, at(7))
    assert daily.is_due(u, at(8)) and daily.is_due(u, at(13))               # catches up if the worker was down
    assert not daily.is_due(u, at(14)) and not daily.is_due(u, at(23))      # but never in the middle of the night


def test_due_respects_the_users_timezone():
    ny = user_obj("America/New_York", 8)                                       # 08:00 EDT == 12:00 UTC in October
    assert not daily.is_due(ny, at(11)) and daily.is_due(ny, at(12)) and daily.is_due(ny, at(16))
    tokyo = user_obj("Asia/Tokyo", 8)                                          # 08:00 JST == 23:00 UTC the day before
    assert daily.is_due(tokyo, at(23, day=2)) and not daily.is_due(tokyo, at(22, day=2))


def test_not_due_twice_on_the_same_local_day():
    u = user_obj(hour=8, last=at(8))
    assert not daily.is_due(u, at(10))
    assert daily.is_due(u, at(8, day=4))                                       # next day is fine


def test_local_day_boundary_uses_local_date():
    ny = user_obj("America/New_York", 20, last=datetime(2026, 10, 4, 1, 0, tzinfo=UTC))    # sent 21:00 local on Oct 3
    assert not daily.is_due(ny, datetime(2026, 10, 4, 1, 30, tzinfo=UTC))                  # still Oct 3 locally
    assert daily.is_due(ny, datetime(2026, 10, 5, 0, 30, tzinfo=UTC))                      # 20:30 on Oct 4 locally


def test_bad_timezone_falls_back_to_utc():
    assert daily.is_due(user_obj("Not/AZone", 8), at(9))


# ── what goes in it ──────────────────────────────────────────────────────
async def test_content_selection(maker, monkeypatch):
    monkeypatch.setattr(get_settings(), "digest_max_matches", 2)
    async with maker() as db:
        u = await make_user(db)
        top, _ = await make_match(db, u.id, score=95)
        mid, _ = await make_match(db, u.id, score=85)
        low, _ = await make_match(db, u.id, score=70)                             # qualifies but beyond the cap
        await make_match(db, u.id, score=60)                                      # below the digest threshold
        await make_match(db, u.id, score=99, status="dismissed")
        await make_match(db, u.id, score=99, digested=True)                       # already emailed
        await make_match(db, u.id, score=99, active=False)
        await make_question(db, u.id, "Pending?"), await make_question(db, u.id, "Sent already?", status="emailed")
        c = await daily.build_content(db, u)
        assert [m.id for m in c.matches] == [top.id, mid.id] and c.more_count == 1
        assert [q.question for q in c.questions] == ["Pending?"]
        assert low.id not in {m.id for m in c.matches}


async def test_flags_control_sections(maker):
    async with maker() as db:
        no_digest = await make_user(db, digest=False)
        no_questions = await make_user(db, question_emails=False)
        for u in (no_digest, no_questions):
            await make_match(db, u.id, score=90)
            await make_question(db, u.id, "Q?")
        a = await daily.build_content(db, no_digest)
        b = await daily.build_content(db, no_questions)
        assert a.matches == [] and len(a.questions) == 1
        assert len(b.matches) == 1 and b.questions == []


# ── sending ──────────────────────────────────────────────────────────────
async def test_send_marks_everything_as_sent(maker, outbox):
    async with maker() as db:
        u = await make_user(db, name="Jane Doe")
        m, job = await make_match(db, u.id, score=88, title="Backend Engineer", company="Stripe")
        q = await make_question(db, u.id, "Have you used Kafka?")
        res = await daily.send_daily(db, u, now=at(9))
        assert res["matches"] == 1 and res["questions"] == 1
        (msg,) = outbox
        assert msg["to"] == u.email and msg["subject"] == "1 new job match + 1 quick question"
        assert "Hi Jane," in msg["text"] and "Stripe" in msg["text"] and "Have you used Kafka?" in msg["text"]
        assert f"/matches/{m.id}" in msg["text"] and job.url in msg["text"]
        assert "<html" in msg["html"].lower() and "Backend Engineer" in msg["html"] and "88" in msg["html"]
        await db.refresh(m), await db.refresh(q), await db.refresh(u)
        assert m.digested_at and q.status == "emailed" and q.emailed_at and u.last_digest_at == at(9)
        # a second run has nothing new to say
        assert await daily.send_daily(db, u, now=at(10)) is None and len(outbox) == 1


async def test_links_are_signed_and_headers_allow_unsubscribe(maker, outbox):
    async with maker() as db:
        u = await make_user(db)
        await make_match(db, u.id), await make_question(db, u.id)
        await daily.send_daily(db, u)
        (msg,) = outbox
        base = get_settings().public_base_url.rstrip("/")
        token = msg["text"].split(f"{base}/q/")[1].split()[0]
        assert read_token("questions", token) == u.id
        unsub = msg["headers"]["List-Unsubscribe"].strip("<>")
        assert unsub.startswith(f"{base}/api/unsubscribe/") and read_token("unsubscribe", unsub.rsplit("/", 1)[1]) == u.id
        assert msg["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click" and unsub in msg["text"]


async def test_nothing_to_send_means_no_email_and_not_marked_sent(maker, outbox):
    async with maker() as db:
        u = await make_user(db)
        await make_match(db, u.id, score=40)
        assert await daily.send_daily(db, u, now=at(9)) is None
        await db.refresh(u)
        assert outbox == [] and u.last_digest_at is None


async def test_preview_sends_but_marks_nothing(maker, outbox):
    async with maker() as db:
        u = await make_user(db)
        await make_match(db, u.id, score=55, digested=True)                 # preview ignores threshold and digested flag
        q = await make_question(db, u.id, status="emailed")
        res = await daily.send_daily(db, u, preview=True)
        assert res["matches"] == 1 and res["questions"] == 1 and outbox[0]["subject"].startswith("[Preview]")
        await db.refresh(q), await db.refresh(u)
        assert q.status == "emailed" and q.emailed_at is None and u.last_digest_at is None


async def test_send_failure_marks_nothing(maker, monkeypatch):
    async def boom(*a, **k):
        raise EmailError("smtp down")

    monkeypatch.setattr(mail, "send_email", boom)
    async with maker() as db:
        u = await make_user(db)
        m, _ = await make_match(db, u.id)
        q = await make_question(db, u.id)
        with pytest.raises(EmailError):
            await daily.send_daily(db, u)
        await db.refresh(m), await db.refresh(q), await db.refresh(u)
        assert m.digested_at is None and q.status == "pending" and u.last_digest_at is None


async def test_matches_are_claimed_before_the_email_goes_out(maker, monkeypatch):
    """A crash (or a concurrent run) after SMTP must not be able to send the same matches again."""
    seen = {}

    async def spy(to, subject, text, html=None, headers=None):
        async with maker() as other:
            seen["digested"] = (await other.scalars(sa.select(JobMatch.digested_at))).one()
            seen["last"] = (await other.scalars(sa.select(User.last_digest_at))).one()

    monkeypatch.setattr(mail, "send_email", spy)
    async with maker() as db:
        u = await make_user(db)
        await make_match(db, u.id)
        await daily.send_daily(db, u, now=at(9))
    assert seen["digested"] == at(9) and seen["last"] == at(9)


@pytest.mark.real_commit
async def test_concurrent_runs_send_one_email(maker, outbox):
    async with maker() as db:
        u = await make_user(db)
        await make_match(db, u.id)

    async def run():
        async with maker() as db:
            user = await db.get(User, u.id)
            return await daily.send_daily(db, user, now=at(9))

    results = await asyncio.gather(*(run() for _ in range(4)))
    assert len(outbox) == 1 and sum(r is not None for r in results) == 1


async def test_failed_send_restores_previous_last_digest_at(maker, monkeypatch):
    async def boom(*a, **k):
        raise EmailError("smtp down")

    monkeypatch.setattr(mail, "send_email", boom)
    async with maker() as db:
        u = await make_user(db)
        u.last_digest_at = at(8, day=2)
        await db.commit()
        await make_match(db, u.id)
        with pytest.raises(EmailError):
            await daily.send_daily(db, u, now=at(9))
        await db.refresh(u)
        assert u.last_digest_at == at(8, day=2)


async def test_html_escapes_untrusted_job_text(maker, outbox):
    async with maker() as db:
        u = await make_user(db)
        await make_match(db, u.id, title="<script>alert(1)</script> Engineer", company='Evil "Corp" & Co')
        await make_question(db, u.id, "Do you <b>know</b> it?")
        await daily.send_daily(db, u)
        html = outbox[0]["html"]
        assert "<script>" not in html and "&lt;script&gt;" in html and "&amp; Co" in html and "<b>know</b>" not in html


async def test_subject_variants(maker, outbox):
    async with maker() as db:
        a = await make_user(db, "a@x.com", question_emails=False)
        await make_match(db, a.id, company="Stripe", title="Backend Engineer"), await make_match(db, a.id, score=70)
        b = await make_user(db, "b@x.com", digest=False)
        await make_question(db, b.id, "One?"), await make_question(db, b.id, "Two?")
        await daily.send_daily(db, a), await daily.send_daily(db, b)
        assert outbox[0]["subject"] == "2 new job matches - top pick: Stripe, Backend Engineer"
        assert outbox[1]["subject"] == "2 quick questions to sharpen your job matches"


# ── the hourly worker task ───────────────────────────────────────────────
async def run_tick(maker, ai=None):
    async with maker() as db:
        t = Task(kind="daily_emails", payload={}, status="queued")
        db.add(t)
        await db.commit()
        tid = t.id
    await run_task(tid, ai or FakeQuestionsAI(), maker)
    async with maker() as db:
        return (await db.get(Task, tid)).result


async def test_tick_sends_only_to_due_users_and_isolates_failures(maker, monkeypatch):
    now = datetime.now(UTC)
    sent, fail_for = [], "bad@x.com"

    async def fake_send(to, subject, text, html=None, headers=None):
        if to == fail_for:
            raise EmailError("rejected")
        sent.append(to)

    monkeypatch.setattr(mail, "send_email", fake_send)
    async with maker() as db:
        due = await make_user(db, "due@x.com", hour=now.hour)
        await make_user(db, "later@x.com", hour=(now.hour + 12) % 24)
        bad = await make_user(db, "bad@x.com", hour=now.hour)
        done = await make_user(db, "done@x.com", hour=now.hour)
        done.last_digest_at = now
        await db.commit()
        for u in (due, bad, done):
            await make_match(db, u.id)
        await make_match(db, (await make_user(db, "empty@x.com", hour=now.hour)).id, score=10)
    out = await run_tick(maker)
    assert sent == ["due@x.com"] and out["sent"] == 1 and out["nothing_to_send"] == 1
    assert set(out["errors"]) == {str(bad.id)}


class NoClaude:
    """Any attribute access is a call to Claude, which daily_emails must never make (#50)."""

    def __getattr__(self, name):
        raise AssertionError(f"daily_emails called Claude ({name})")


async def test_tick_sends_the_collected_questions_without_calling_claude(maker, outbox):
    now = datetime.now(UTC)
    async with maker() as db:
        ok = await make_user(db, "ok@x.com", hour=now.hour)
        await make_question(db, ok.id, "Kafka?")
        await make_match(db, ok.id, score=70, confidence=0.4, unknowns=[unknown("Not yet collected?")])
    out = await run_tick(maker, NoClaude())
    assert out["sent"] == 1 and out["errors"] == {} and "1 quick question" in outbox[0]["subject"]
    assert "Kafka?" in outbox[0]["text"] and "Not yet collected?" not in outbox[0]["text"]
    async with maker() as db:
        assert (await db.scalars(sa.select(ClarifyingQuestion.status))).one() == "emailed"


# ── collecting questions ahead of the send hour (#50) ────────────────────
def test_questions_are_due_in_the_hour_before_the_digest(monkeypatch):
    monkeypatch.setattr(get_settings(), "questions_lead_hours", 1)
    u = user_obj(hour=8)
    assert daily.questions_due(u, at(7)) == at(8, minute=0)
    assert daily.questions_due(u, at(7, minute=0)) == at(8, minute=0)
    assert daily.questions_due(u, at(6)) is None and daily.questions_due(u, at(8)) is None
    midnight = user_obj(hour=0)                                                # ahead of tomorrow's 00:00
    assert daily.questions_due(midnight, at(23)) == at(0, day=4, minute=0)
    tokyo = user_obj("Asia/Tokyo", 8)                                          # 08:00 JST == 23:00 UTC the day before
    assert daily.questions_due(tokyo, at(22, day=2)) == at(23, day=2, minute=0)
    assert daily.questions_due(tokyo, at(23, day=2)) is None
    monkeypatch.setattr(get_settings(), "questions_lead_hours", 3)
    assert daily.questions_due(u, at(5)) == daily.questions_due(u, at(7)) == at(8, minute=0)
    assert daily.questions_due(user_obj(hour=None), at(7)) is None
    monkeypatch.setattr(get_settings(), "questions_lead_hours", 0)             # 0 turns it off
    assert daily.questions_due(u, at(7, minute=59)) is None


async def test_question_collection_is_queued_once_per_digest_for_opted_in_users(maker, monkeypatch, published):
    from jobfinder.services import digest

    monkeypatch.setattr(get_settings(), "questions_lead_hours", 1)
    async with maker() as db:
        on = await make_user(db, "on@x.com", hour=8)
        off = await make_user(db, "off@x.com", hour=8, question_emails=False)
        later = await make_user(db, "later@x.com", hour=12)
        await make_user(db, "nothing-to-review@x.com", hour=8)
        for u in (on, off, later):
            await make_match(db, u.id, confidence=0.4, unknowns=[unknown("Kafka?")])
        assert await digest.schedule_questions(db, at(7)) == 1
        await db.commit()
        assert await digest.schedule_questions(db, at(7, minute=30)) == 0       # still queued
        rows = (await db.scalars(sa.select(Task).where(Task.kind == "collect_questions"))).all()
        assert [(t.user_id, t.payload, t.queue) for t in rows] == [(on.id, {}, "ai")]
        # One dedupe key with the planner's and match_user's rows, so it never runs beside one of them.
        assert (await ledger.add(db, "collect_questions", user_id=on.id, dedupe=True)).id == rows[0].id
        rows[0].status = "done"
        await db.commit()
        assert await digest.schedule_questions(db, at(7, minute=55)) == 0       # done counts too: once per digest
        assert await digest.schedule_questions(db, at(7, day=4)) == 1           # tomorrow's digest gets its own


async def test_a_collection_already_due_in_the_window_counts(maker, monkeypatch, published):
    from jobfinder.services import digest

    monkeypatch.setattr(get_settings(), "questions_lead_hours", 2)
    async with maker() as db:
        u = await make_user(db, hour=8)
        await make_match(db, u.id, confidence=0.4, unknowns=[unknown("Kafka?")])
        planned = await ledger.add(db, "collect_questions", user_id=u.id, run_after=at(6, minute=30), dedupe=True)
        planned.status = "done"
        await db.commit()
        assert await digest.schedule_questions(db, at(7)) == 0                  # the planner's 06:30 run did it


async def test_questions_collected_ahead_go_out_in_the_digest(maker, monkeypatch, published, outbox):
    """The whole flow on the ledger runtime: at H-1 the hourly tick queues collect_questions (once), the ai
    handler collects them, and at H the tick sends them without Claude."""
    from jobfinder.core import clock
    from jobfinder.tasks import io, runtime
    from tests.fakes import FakeClock

    monkeypatch.setattr(get_settings(), "questions_lead_hours", 1)
    fake_now = FakeClock(at(7))
    monkeypatch.setattr(clock, "utcnow", fake_now)
    async with maker() as db:
        u = await make_user(db, "q@x.com", hour=8, digest=False)
        await make_match(db, u.id, score=70, confidence=0.4, unknowns=[unknown("Used Kafka?")])

    async def tick():
        async with maker() as db:
            t = await ledger.add(db, "daily_emails", dedupe=True)
            await db.commit()
        await runtime.execute("daily_emails", io.daily_emails, t.id, wants_ai=True, ai=NoClaude())
        async with maker() as db:
            return (await db.get(Task, t.id)).result

    first = await tick()
    assert first["questions_queued"] == 1 and first["due"] == 0 and not outbox
    assert (await tick())["questions_queued"] == 0                              # a second run that hour doesn't double
    async with maker() as db:
        (cq,) = (await db.scalars(sa.select(Task).where(Task.kind == "collect_questions"))).all()
    ai = FakeQuestionsAI()
    await runtime.execute("collect_questions", runtime.REGISTRY["collect_questions"], cq.id, ai=ai)
    assert len(ai.calls) == 1

    fake_now.now = at(8)
    out = await tick()
    assert out["sent"] == 1 and out["questions_queued"] == 0
    (msg,) = outbox
    assert "Used Kafka?" in msg["text"] and "1 quick question" in msg["subject"]
    async with maker() as db:
        assert (await db.scalars(sa.select(ClarifyingQuestion.status))).one() == "emailed"


async def test_email_has_no_apply_link_for_unsafe_job_urls(maker, outbox):
    async with maker() as db:
        u = await make_user(db)
        await make_match(db, u.id, title="Evil Job", url="javascript:alert(1)")
        await make_match(db, u.id, title="Good Job", url="https://jobs.example/good")
        await daily.send_daily(db, u, now=at(9))
    (msg,) = outbox
    assert "javascript:" not in msg["text"] and "javascript:" not in msg["html"]
    assert msg["text"].count("Apply:") == 1 and "https://jobs.example/good" in msg["html"]


async def test_plain_text_email_is_not_html_escaped_but_html_is(maker, outbox):
    async with maker() as db:
        u = await make_user(db)
        await make_match(db, u.id, title="R&D <b>Engineer</b>", company="AT&T", url="https://boards.example/j?gh_jid=1&gh_src=x")
        await daily.send_daily(db, u, now=at(9))
    (msg,) = outbox
    assert "AT&T" in msg["text"] and "&amp;" not in msg["text"] and "?gh_jid=1&gh_src=x" in msg["text"]
    assert "AT&amp;T" in msg["html"] and "<b>Engineer</b>" not in msg["html"] and "&lt;b&gt;" in msg["html"]


def test_catch_up_window_crosses_local_midnight():
    u = user_obj(hour=22)
    assert not daily.is_due(u, at(21, minute=59))
    assert daily.is_due(u, at(22)) and daily.is_due(u, at(23, minute=30))
    assert daily.is_due(u, at(0, day=4, minute=30))                       # missed 22:05 and 23:05, still sent at 00:30
    assert daily.is_due(u, at(3, day=4, minute=59))
    assert not daily.is_due(u, at(4, day=4, minute=0))                    # window (6h) is over
    sent = user_obj(hour=22, last=at(22, minute=10))
    assert not daily.is_due(sent, at(0, day=4, minute=30))                # already sent for that evening
    assert daily.is_due(sent, at(22, day=4))                              # next evening is fine


def test_catch_up_window_across_midnight_in_another_timezone():
    tokyo = user_obj("Asia/Tokyo", 23)                                    # 23:00 JST == 14:00 UTC
    assert daily.is_due(tokyo, at(15, day=3, minute=30)) and daily.is_due(tokyo, at(19, day=3))   # 00:30 and 04:00 JST next day
    assert not daily.is_due(tokyo, at(13, day=3, minute=59)) and not daily.is_due(tokyo, at(20, day=3))
