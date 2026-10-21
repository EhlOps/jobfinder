"""Per-board ingest (#50): ingest_tick queues one ingest_board per due board (and one ingest_source per
due extra source), each board runs and fails on its own, and the planner is queued once when jobs
changed."""
import asyncio
import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from jobfinder.core.config import get_settings
from jobfinder.models import Company, Job, Task
from jobfinder.services import ingestion
from jobfinder.tasks import io as io_tasks
from jobfinder.tasks import ledger, runtime
from jobfinder.tasks.runtime import PAYLOADS, REGISTRY
from tests.fakes import serve

GREENHOUSE = json.loads((Path(__file__).parent / "fixtures" / "greenhouse_discord.json").read_text())
API = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
# beta's own postings: the same ones twice would dedupe across the boards
BETA = {**GREENHOUSE, "jobs": [{**j, "id": j["id"] + 1, "title": j["title"] + " II",
                                "absolute_url": j["absolute_url"] + "1"} for j in GREENHOUSE["jobs"]]}


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    for name, value in [("adaptive_schedule_enabled", False), ("enable_jobspy", False), ("usajobs_api_key", "")]:
        monkeypatch.setattr(s, name, value)
    return s


@pytest.fixture
def boards(web):
    """Two greenhouse boards that answer, one that is gone (404)."""
    serve(web, API.format("alpha"), json=GREENHOUSE)
    serve(web, API.format("beta"), json=BETA)
    serve(web, API.format("gone"), 404)
    return ("alpha", "beta", "gone")


async def _seed(maker, slugs) -> dict[str, int]:
    async with maker() as db:
        rows = [Company(name=s.title(), ats="greenhouse", slug=s) for s in slugs]
        db.add_all(rows)
        await db.commit()
        return {c.slug: c.id for c in rows}


async def _run(kind: str, task_id: int) -> None:
    await runtime.execute(kind, REGISTRY[kind], task_id, payload_model=PAYLOADS[kind], wants_ai=False)


async def _queued(maker, kind: str, payload: dict | None = None) -> int:
    async with maker() as db:
        t = await ledger.add(db, kind, payload)
        await db.commit()
        return t.id


async def _tasks(maker, kind: str | None = None) -> list[Task]:
    async with maker() as db:
        q = sa.select(Task).order_by(Task.id)
        return list(await db.scalars(q.where(Task.kind == kind) if kind else q))


async def _extra_runs(maker) -> int:
    from jobfinder.models import RateEvent

    async with maker() as db:
        return await db.scalar(sa.select(sa.func.count()).where(RateEvent.kind == ingestion.EXTRA_KIND))


@pytest.mark.real_commit
async def test_the_tick_fans_out_and_one_failing_board_fails_nothing_else(maker, published, settings, boards,
                                                                          monkeypatch):
    ids = await _seed(maker, boards)
    monkeypatch.setattr(settings, "usajobs_api_key", "k")
    monkeypatch.setattr(settings, "usajobs_email", "ops@example.com")
    extras = []

    async def fake_extra(_, source):
        extras.append(source)
        return ingestion.IngestStats(source=source, company="(usajobs.gov)", fetched=0)

    monkeypatch.setattr(ingestion, "ingest_extra", fake_extra)

    tick = await _queued(maker, "ingest_tick")
    await _run("ingest_tick", tick)
    [row] = await _tasks(maker, "ingest_tick")
    assert row.status == "done" and row.result == {"due": 3, "sources": ["usajobs"]}
    board_rows = await _tasks(maker, "ingest_board")
    assert sorted(t.payload["company_id"] for t in board_rows) == sorted(ids.values())
    assert len({t.dedupe_key for t in board_rows}) == 3 and all(t.queue == "io" for t in board_rows)
    [source] = await _tasks(maker, "ingest_source")
    assert source.payload == {"source": "usajobs"}
    # committed with the tick and published after it
    await published.settle()
    assert {(k, i) for _, k, i in published} >= {("ingest_board", t.id) for t in board_rows} | {("ingest_source", source.id)}

    assert await _extra_runs(maker) == 0  # recorded by the source task, not the tick

    # a second tick while they're still queued doesn't double them
    await _run("ingest_tick", await _queued(maker, "ingest_tick"))
    assert len(await _tasks(maker, "ingest_board")) == 3 and len(await _tasks(maker, "ingest_source")) == 1

    await asyncio.gather(*(_run("ingest_board", t.id) for t in board_rows), _run("ingest_source", source.id))
    by_company = {t.payload["company_id"]: t for t in await _tasks(maker, "ingest_board")}
    assert all(t.status == "done" for t in by_company.values())  # the board's failure is the board's
    assert by_company[ids["gone"]].result["error"] == "board not found (check the slug)"
    for slug in ("alpha", "beta"):
        r = by_company[ids[slug]].result
        assert r["error"] is None and r["new"] == len(GREENHOUSE["jobs"])
    async with maker() as db:
        gone = await db.get(Company, ids["gone"])
        assert gone.consecutive_failures == 1 and gone.last_fetched_at is not None
        per_board = dict((await db.execute(sa.select(Job.company_id, sa.func.count()).group_by(Job.company_id))).all())
    assert per_board == {ids["alpha"]: len(GREENHOUSE["jobs"]), ids["beta"]: len(GREENHOUSE["jobs"])}
    assert extras == ["usajobs"] and (await _tasks(maker, "ingest_source"))[0].status == "done"
    assert await _extra_runs(maker) == 1
    # every board asked for a plan (the adaptive schedule is off): one queued row, a little later
    [plan] = await _tasks(maker, "plan_work")
    assert plan.status == "queued" and plan.run_after > plan.created_at


@pytest.mark.real_commit
async def test_a_hanging_board_does_not_delay_the_others(maker, published, settings, web, monkeypatch):
    ids = await _seed(maker, ("alpha", "slow"))
    serve(web, API.format("alpha"), json=GREENHOUSE)
    started, release = asyncio.Event(), asyncio.Event()
    real = ingestion.ATS_FETCHERS["greenhouse"]

    async def fetch(client, slug, name):
        if slug == "slow":
            started.set()
            await release.wait()
            return []
        return await real(client, slug, name)

    monkeypatch.setitem(ingestion.ATS_FETCHERS, "greenhouse", fetch)
    await _run("ingest_tick", await _queued(maker, "ingest_tick"))
    rows = {t.payload["company_id"]: t.id for t in await _tasks(maker, "ingest_board")}
    slow = asyncio.create_task(_run("ingest_board", rows[ids["slow"]]))
    await started.wait()
    await _run("ingest_board", rows[ids["alpha"]])  # finishes while the slow one still hangs
    status = {t.payload["company_id"]: t.status for t in await _tasks(maker, "ingest_board")}
    assert status == {ids["alpha"]: "done", ids["slow"]: "running"}
    release.set()
    await slow
    assert all(t.status == "done" for t in await _tasks(maker, "ingest_board"))


async def test_the_tick_queues_only_due_boards_with_the_adaptive_schedule(maker, published, settings, monkeypatch):
    from datetime import timedelta

    from jobfinder.core.clock import utcnow

    monkeypatch.setattr(settings, "adaptive_schedule_enabled", True)
    ids = await _seed(maker, ("due", "later"))
    async with maker() as db:
        (await db.get(Company, ids["later"])).next_fetch_at = utcnow() + timedelta(hours=1)
        await db.commit()
    await _run("ingest_tick", await _queued(maker, "ingest_tick"))
    assert [t.payload for t in await _tasks(maker, "ingest_board")] == [{"company_id": ids["due"]}]


async def test_an_unchanged_board_does_not_queue_the_planner(maker, published, settings, monkeypatch):
    monkeypatch.setattr(settings, "adaptive_schedule_enabled", True)  # off it, every run plans
    async def same(client, slug, name):
        return []

    monkeypatch.setitem(ingestion.ATS_FETCHERS, "greenhouse", same)
    ids = await _seed(maker, ("quiet",))
    await _run("ingest_board", await _queued(maker, "ingest_board", {"company_id": ids["quiet"]}))
    assert await _tasks(maker, "plan_work") == []


async def test_a_board_removed_since_the_tick_is_skipped(maker, published):
    await _run("ingest_board", await _queued(maker, "ingest_board", {"company_id": 999_999}))
    [row] = await _tasks(maker, "ingest_board")
    assert row.status == "done" and row.result["error"].startswith("skipped")


def test_board_payloads_validate():
    assert io_tasks.IngestBoardArgs.model_validate({"company_id": "4", "old": 1}).model_dump() == {"company_id": 4}
    assert io_tasks.IngestSourceArgs.model_validate({"source": "jobspy"}).source == "jobspy"
