"""The handlers on @ledger_task (#48): every kind has one, their payload models accept every row queued
before the switch, and their follow-ups go through the ledger."""
import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from jobfinder.models import Task
from jobfinder.tasks import ai as ai_tasks
from jobfinder.tasks import io as io_tasks
from jobfinder.tasks import ledger, runtime
from jobfinder.tasks.kinds import QUEUES
from tests.fakes import run_task


def test_every_ai_and_io_kind_has_a_handler():
    assert {k for k, q in QUEUES.items() if q != "render"} <= set(runtime.REGISTRY)


@pytest.mark.parametrize("model, payload, expected", [
    (ai_tasks.CoverLetterArgs, {"match_id": 3}, {"match_id": 3, "tone": "professional", "notes": ""}),
    (ai_tasks.CoverLetterArgs, {"match_id": "3", "tone": "warm", "notes": "hi", "old": 1},
     {"match_id": 3, "tone": "warm", "notes": "hi"}),
    (ai_tasks.ResumeTailorArgs, {"match_id": 5}, {"match_id": 5, "force": False}),
    (ai_tasks.ResumeTailorArgs, {"match_id": 5, "force": True}, {"match_id": 5, "force": True}),
    (io_tasks.ResolveLinkArgs, {"link_id": 7}, {"link_id": 7}),
    (io_tasks.ScoreCovetedArgs, {}, {"no_audit": False}),
    (io_tasks.AuditCoverageArgs, {}, {"company_id": None, "target_id": None}),
    (io_tasks.AuditCoverageArgs, {"target_id": 2}, {"company_id": None, "target_id": 2}),
    (ai_tasks.InduceRecipeArgs, {"link_id": 7}, {"link_id": 7, "samples": [], "tried": [], "notes": [],
                                                 "single_job": None, "board_error": None, "attempt": 0}),
    (ai_tasks.InduceRecipeArgs, {"link_id": 7, "samples": [["https://a.com/api", "{}"]]},
     {"link_id": 7, "samples": [("https://a.com/api", "{}")], "tried": [], "notes": [], "single_job": None,
      "board_error": None, "attempt": 0}),
    (ai_tasks.ReinduceArgs, {"company_id": 4}, {"company_id": 4, "attempt": 0}),
])
def test_payloads_queued_today_validate(model, payload, expected):
    assert model.model_validate(payload).model_dump() == expected


def test_a_payload_missing_its_id_is_refused():
    with pytest.raises(ValidationError):
        io_tasks.ResolveLinkArgs.model_validate({})
    with pytest.raises(ValidationError):
        ai_tasks.InduceRecipeArgs.model_validate({})


async def test_the_runtime_hands_over_the_validated_payload(maker, monkeypatch):
    from jobfinder.services import letters

    got = []

    async def generate(db, ai, user_id, match_id, tone, notes):
        got.append((match_id, tone, notes))
        return {"ok": True}

    monkeypatch.setattr(letters, "generate", generate)
    async with maker() as db:
        task = await ledger.add(db, "cover_letter", {"match_id": 9})
        await db.commit()
    await run_task(task.id, ai=object(), session_factory=maker)
    async with maker() as db:
        task = await db.get(Task, task.id)
    assert (task.status, task.result) == ("done", {"ok": True}) and got == [(9, "professional", "")]


async def test_follow_ups_are_ledger_rows_flushed_into_the_task(db, monkeypatch):
    """score_coveted queues its audit with ledger.add: the row carries its queue and is only flushed, so
    it commits (and is published) with the task's result."""
    from jobfinder.services import coverage

    async def score_all(db):
        return {"scored": 0}

    monkeypatch.setattr(coverage, "score_all", score_all)
    await io_tasks.score_coveted(db, None, Task(kind="score_coveted", payload={}), io_tasks.ScoreCovetedArgs())
    [audit] = (await db.scalars(sa.select(Task).where(Task.kind == "audit_coverage"))).all()
    assert (audit.queue, audit.status) == ("io", "queued")
    assert ledger.PENDING in db.info  # published once the runtime commits
