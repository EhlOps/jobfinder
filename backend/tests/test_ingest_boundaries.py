"""Ingestion commits only at its boundaries (#46): each board in ingest_boards, the task handlers, the CLI.

Each test fails a run part-way and shows that nothing past the last boundary (board, checkpoint, task)
was kept, while what came before it was.
"""
import asyncio

import httpx
import pytest
import sqlalchemy as sa

from jobfinder.models import Company, CovetedTarget, Job, JobLink, Task
from jobfinder.services import board_validation, coverage, ingestion, resolver
from jobfinder.services.board_validation import ValidationResult, add_board
from jobfinder.tasks import io
from tests.rollback import Boom, count, raise_after
from tests.test_ingest_service import fake_fetcher, posting
from tests.test_job_links import GOOD, _link, board_json


@pytest.mark.real_commit
async def test_a_board_failing_after_its_upsert_keeps_only_the_other_boards(maker, monkeypatch):
    """ingest_boards commits per board: B's upserted jobs roll back with its failure, A's stay."""
    monkeypatch.setitem(ingestion.ATS_FETCHERS, "greenhouse", fake_fetcher([posting(1)]))
    monkeypatch.setitem(ingestion.ATS_FETCHERS, "lever", fake_fetcher([posting(2, source="lever")]))
    real = ingestion.deactivate_missing

    async def fail_for_lever(db, source, company_id, seen):
        n = await real(db, source, company_id, seen)
        if source == "lever":
            raise Boom("after the upsert")
        return n

    monkeypatch.setattr(ingestion, "deactivate_missing", fail_for_lever)
    async with maker() as db:
        db.add_all([Company(name="A", ats="greenhouse", slug="a"), Company(name="B", ats="lever", slug="b")])
        await db.commit()
    results = {r.company: r for r in await ingestion.ingest_boards(maker)}
    assert results["A"].error is None and "Boom" in results["B"].error
    assert await count(maker, Job, source="greenhouse") == 1
    assert await count(maker, Job, source="lever") == 0  # upserted, then rolled back with the failure
    async with maker() as db:
        b = await db.scalar(sa.select(Company).where(Company.slug == "b"))
    assert b.consecutive_failures == 1  # the failure itself is still recorded and committed


async def test_ingest_company_leaves_the_commit_to_its_caller(maker, monkeypatch):
    monkeypatch.setitem(ingestion.ATS_FETCHERS, "greenhouse", fake_fetcher([posting(1)]))
    async with maker() as db, httpx.AsyncClient() as client:
        db.add(c := Company(name="A", ats="greenhouse", slug="a"))
        await db.commit()
        await ingestion.ingest_company(db, client, c)
        assert await db.scalar(sa.select(sa.func.count()).select_from(Job)) == 1
        await db.rollback()  # no boundary commit: nothing was kept
    assert await count(maker, Job) == 0


async def test_failed_resolve_keeps_only_the_link_outcome(maker, monkeypatch, web):
    """The board add_board wrote is undone with the failed resolve; the link is marked and committed."""
    raise_after(monkeypatch, resolver, "add_board")
    async with maker() as db:
        link = await _link(db)
        task = Task(kind="resolve_job_link", payload={"link_id": link.id})
        monkeypatch.setattr(resolver, "httpx", _ClientFor(board_json(web, GOOD)))
        out = await io.resolve_job_link(db, None, task, io.ResolveLinkArgs(link_id=link.id))
    assert out == {"error": "Boom"}
    assert await count(maker, Company, slug="acme") == 0
    async with maker() as db:
        row = await db.get(JobLink, link.id)
    assert row.status == "unresolved" and "went wrong" in row.error


class _ClientFor:
    """Stands in for the httpx module in resolver so resolve_link's own client is the test's."""

    def __init__(self, client):
        self.AsyncClient = lambda **kw: client


async def test_audit_keeps_targets_up_to_the_last_checkpoint(maker, monkeypatch):
    """A crash mid-audit (here a cancellation) keeps every target finished before it and nothing after."""
    async def fake(db, target, client):
        target.last_error = f"done {target.domain}"
        if target.domain == "b.com":
            raise asyncio.CancelledError  # not an Exception: the audit's per-target guard lets it through
        return {"target_id": target.id, "status": "unresolved"}

    monkeypatch.setattr(coverage, "resolve_target", fake)
    async with maker() as db:
        db.add_all([CovetedTarget(name="A", domain="a.com"), CovetedTarget(name="B", domain="b.com")])
        await db.commit()
        with pytest.raises(asyncio.CancelledError):
            await io.audit_coverage(db, None, Task(kind="audit_coverage"), io.AuditCoverageArgs())
        await db.rollback()  # what the worker does with a task that blew up
    async with maker() as db:
        errors = dict((await db.execute(sa.select(CovetedTarget.domain, CovetedTarget.last_error))).all())
    assert errors == {"a.com": "done a.com", "b.com": None}


async def test_audit_marks_a_failing_target_without_its_partial_writes(maker, monkeypatch):
    async def fake(db, target, client):
        target.name = "half-written"
        raise RuntimeError("bug")

    monkeypatch.setattr(coverage, "resolve_target", fake)
    async with maker() as db:
        db.add(CovetedTarget(name="X", domain="x.com"))
        await db.commit()
        out = await io.audit_coverage(db, None, Task(kind="audit_coverage"), io.AuditCoverageArgs())
    async with maker() as db:
        t = await db.scalar(sa.select(CovetedTarget))
    assert out["results"][0]["status"] == "error"
    assert t.name == "X" and "RuntimeError" in t.last_error


async def test_lost_insert_race_keeps_the_callers_earlier_writes(maker, monkeypatch):
    """add_board's IntegrityError retry is a savepoint: only its own insert is undone."""
    async def never_found(db, ats, slug):
        return None

    async with maker() as db:
        db.add(Company(name="Acme", ats="greenhouse", slug="acme"))
        await db.commit()
        monkeypatch.setattr(board_validation.boards_repo, "existing", never_found)
        db.add(Company(name="Other", ats="lever", slug="other"))  # the caller's own pending write
        company, reason = await add_board(db, "greenhouse", "acme", "Acme", validation=ValidationResult(True))
        assert company is None and reason.startswith("duplicate")
        await db.commit()
    assert await count(maker, Company, slug="other") == 1


async def test_discovery_keeps_boards_added_before_a_failure(maker, monkeypatch):
    """discover_boards checkpoints after each added board (the task passes its commit)."""
    from jobfinder.services import discovery

    async def found(_fetcher):
        return [("greenhouse", "a"), ("greenhouse", "b")]

    async def all_valid(fetcher, batch):
        return [ValidationResult(True, "", 1) for _ in batch]

    real, calls = discovery.add_board, []

    async def second_fails(*a, **kw):
        out = await real(*a, **kw)
        calls.append(a[2])
        if len(calls) == 2:
            raise Boom("after the second board")
        return out

    monkeypatch.setattr(discovery, "validate_candidates", all_valid)
    monkeypatch.setattr(discovery, "add_board", second_fails)
    monkeypatch.setattr(discovery.random, "shuffle", lambda xs: None)
    src = discovery.Source("fake", "n/a", True, found)
    async with maker() as db, httpx.AsyncClient() as client:
        with pytest.raises(Boom):
            await discovery.discover_boards(db, client, sources=[src], checkpoint=db.commit)
        await db.rollback()  # what the worker does with a task that blew up
    assert await count(maker, Company, slug=calls[0]) == 1
    assert await count(maker, Company, slug=calls[1]) == 0


async def test_score_coveted_commits_at_the_task(maker, monkeypatch):
    raise_after(monkeypatch, coverage, "score_all")
    async with maker() as db:
        db.add(Company(name="A", ats="greenhouse", slug="a", coveted_score=42.0))
        await db.commit()
        with pytest.raises(Boom):
            await io.score_coveted(db, None, Task(kind="score_coveted"), io.ScoreCovetedArgs(no_audit=True))
        await db.rollback()
    async with maker() as db:
        c = await db.scalar(sa.select(Company))
    assert c.coveted_score == 42.0  # scored in the session, never committed
