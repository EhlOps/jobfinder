import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder import worker
from jobfinder.config import get_settings
from jobfinder.models import Task
from jobfinder.scheduling.handlers import HANDLERS


def test_scheduler_has_ingest_and_daily_email_jobs(monkeypatch):
    monkeypatch.setattr(get_settings(), "ingest_interval_hours", 3)
    sched = worker.make_scheduler()
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {"ingest_jobs", "daily_emails"}
    assert jobs["ingest_jobs"].args == ("ingest_jobs",) and jobs["daily_emails"].args == ("daily_emails",)
    assert "interval[3:00:00]" in str(jobs["ingest_jobs"].trigger)
    assert "minute='5'" in str(jobs["daily_emails"].trigger) and "hour" not in str(jobs["daily_emails"].trigger).replace("minute", "")


async def test_scheduled_work_goes_through_the_queue_without_piling_up(engine, monkeypatch):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", maker)
    await worker.enqueue_scheduled("ingest_jobs")
    await worker.enqueue_scheduled("ingest_jobs")            # previous run still queued: not duplicated
    await worker.enqueue_scheduled("daily_emails")
    async with maker() as db:
        kinds = sorted((await db.scalars(sa.select(Task.kind))).all())
    assert kinds == ["daily_emails", "ingest_jobs"]


def test_every_scheduled_kind_has_a_handler():
    assert {"ingest_jobs", "daily_emails", "collect_questions", "match_user"} <= set(HANDLERS)
