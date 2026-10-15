from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa

from jobfinder.ingest import http_safety
from jobfinder.ingest.recipes.health import (
    ACTIVE,
    DEGRADED,
    DISABLED,
    VALIDATED,
    needs_reinduce,
    record_reinduce,
    record_run,
)
from jobfinder.ingest.service import ingest_company
from jobfinder.models import Company, Job

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def board(state=VALIDATED, baseline=100, **health):
    return Company(name="Acme", ats="recipe", slug="acme.com", enabled=True, recipe={"kind": "json_api"},
                   recipe_state=state, recipe_health={"baseline_yield": baseline, **health})


# (start state, baseline, runs as (n_postings, error), expected states after each run)
CASES = [
    ("validated -> active on first healthy run", VALIDATED, 100, [(98, None)], [ACTIVE]),
    ("induced -> active", "induced", 0, [(5, None)], [ACTIVE]),
    ("no state yet behaves as validated", None, None, [(5, None)], [ACTIVE]),
    ("one empty run is tolerated", ACTIVE, 100, [(0, None)], [ACTIVE]),
    ("two empty runs degrade", ACTIVE, 100, [(0, None), (0, None)], [ACTIVE, DEGRADED]),
    ("an error counts like an empty run", ACTIVE, 100, [(0, "HTTP 404"), (0, None)], [ACTIVE, DEGRADED]),
    ("empty streak resets on a good run", ACTIVE, 100, [(0, None), (90, None), (0, None)],
     [ACTIVE, ACTIVE, ACTIVE]),
    ("drop of more than 60% degrades", ACTIVE, 100, [(39, None)], [DEGRADED]),
    ("drop of exactly 60% is fine", ACTIVE, 100, [(40, None)], [ACTIVE]),
    ("recovers when yield is back", ACTIVE, 100, [(10, None), (10, None), (95, None)],
     [DEGRADED, DEGRADED, ACTIVE]),
    ("recovers from zero-degraded", ACTIVE, 100, [(0, None), (0, None), (0, None), (80, None)],
     [ACTIVE, DEGRADED, DEGRADED, ACTIVE]),
    ("validated board that is empty twice degrades", VALIDATED, 10, [(0, None), (0, None)],
     [VALIDATED, DEGRADED]),
    ("disabled stays disabled", DISABLED, 100, [(100, None), (0, None)], [DISABLED, DISABLED]),
    ("no baseline: any yield is healthy", ACTIVE, 0, [(1, None)], [ACTIVE]),
]


@pytest.mark.parametrize(("start", "baseline", "runs", "expected"), [c[1:] for c in CASES],
                         ids=[c[0] for c in CASES])
def test_state_machine(start, baseline, runs, expected):
    c = board(start, baseline)
    got = [record_run(c, n, err, now=T0) for n, err in runs]
    assert got == expected
    assert c.recipe_state == expected[-1]


def test_health_fields_are_recorded():
    c = board(ACTIVE, 100)
    record_run(c, 0, "HTTP 500: " + "x" * 1000, now=T0)
    h = c.recipe_health
    assert h["last_yield"] == 0 and h["zero_runs"] == 1 and len(h["last_error"]) == 300
    assert h.get("degraded_since") is None
    record_run(c, 0, None, now=T0)
    assert c.recipe_health["degraded_since"] == T0.isoformat() and c.recipe_health["last_error"] is None
    record_run(c, 0, None, now=T0 + timedelta(days=1))  # still degraded: the start time is kept
    assert c.recipe_health["degraded_since"] == T0.isoformat()
    record_run(c, 100, None, now=T0 + timedelta(days=2))
    assert c.recipe_health["degraded_since"] is None and c.recipe_health["zero_runs"] == 0


@pytest.mark.parametrize(("baseline", "runs", "expected"), [
    (100, [120], 120),  # a new high is taken at once
    (100, [80], 96),  # healthy lower yield: EMA 0.8*100 + 0.2*80
    (100, [50, 50, 50], 76),  # a real shrink drifts the baseline down: 90, 82, 76
    (100, [10], 100),  # a degraded yield never moves the baseline
    (0, [7], 7),
])
def test_baseline_is_max_of_yield_and_ema(baseline, runs, expected):
    c = board(ACTIVE, baseline)
    for n in runs:
        record_run(c, n, now=T0)
    assert c.recipe_health["baseline_yield"] == expected


def test_reinduce_cap_two_per_seven_days():
    c = board(ACTIVE, 100)
    assert not needs_reinduce(c, now=T0)  # active: nothing to do
    record_run(c, 0, now=T0)
    record_run(c, 0, now=T0)
    assert needs_reinduce(c, now=T0)
    record_reinduce(c, True, {"kind": "json_api", "v": 2}, now=T0)
    assert c.recipe_state == VALIDATED and c.recipe == {"kind": "json_api", "v": 2}
    assert c.recipe_health["baseline_yield"] is None and c.recipe_health["reinduce_count_7d"] == 1
    assert not needs_reinduce(c, now=T0)  # validated, not degraded
    record_run(c, 30, now=T0)
    assert c.recipe_state == ACTIVE and c.recipe_health["baseline_yield"] == 30
    record_run(c, 0, now=T0)
    record_run(c, 0, now=T0)
    assert needs_reinduce(c, now=T0 + timedelta(days=1))
    record_reinduce(c, True, {"kind": "json_api", "v": 3}, now=T0 + timedelta(days=1))
    record_run(c, 0, now=T0)
    record_run(c, 0, now=T0)
    assert c.recipe_state == DEGRADED
    assert not needs_reinduce(c, now=T0 + timedelta(days=2))  # cap of 2 reached in this window
    assert needs_reinduce(c, now=T0 + timedelta(days=7))  # a new window
    assert c.recipe_health["reinduce_count_7d"] == 2  # needs_reinduce never mutates


def test_failed_reinduce_disables_the_board():
    c = board(ACTIVE, 100)
    record_run(c, 0, "HTTP 404", now=T0)
    record_run(c, 0, "HTTP 404", now=T0)
    assert record_reinduce(c, False, None, now=T0) == DISABLED
    assert c.enabled is False and c.recipe_state == DISABLED
    assert "re-induction failed" in c.disabled_reason and "HTTP 404" in c.disabled_reason
    assert not needs_reinduce(c, now=T0 + timedelta(days=30))
    c2 = board(DEGRADED, 100)
    record_reinduce(c2, True, None, now=T0)  # "ok" without a recipe is not a success
    assert c2.recipe_state == DISABLED


def test_disabled_company_never_needs_reinduce():
    c = board(DEGRADED, 100)
    c.enabled, c.disabled_reason = False, "switched off by an admin"
    assert not needs_reinduce(c, now=T0)


def test_failure_streak_auto_disable_can_still_heal():
    c = board(DEGRADED, 100)
    c.enabled, c.consecutive_failures = False, 5
    c.disabled_reason, c.last_error = "auto-disabled after 5 consecutive failures: HTTP 404", "HTTP 404"
    assert needs_reinduce(c, now=T0)
    record_reinduce(c, True, {"kind": "html"}, now=T0)
    assert c.enabled and c.disabled_reason is None and c.consecutive_failures == 0 and c.last_error is None


# ── hooked into ingest_company ────────────────────────────────────────────
RECIPE = {
    "kind": "json_api", "list": {"url": "https://careers.acme.com/api/jobs"}, "items": "$.jobs[*]",
    "fields": {"external_id": "$.id", "title": "$.title", "url": "https://careers.acme.com/jobs/{external_id}"},
}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def fake(url):
        return httpx.URL(url).host, "93.184.216.34"

    monkeypatch.setattr(http_safety, "resolve_public", fake)


def api(responses):
    """Serves the next canned response per call: that many jobs, or an HTTP status given as a string."""
    it = iter(responses)

    def handler(request):
        r = next(it)
        if isinstance(r, str):
            return httpx.Response(int(r))
        return httpx.Response(200, json={"jobs": [{"id": i, "title": f"Job {i}"} for i in range(r)]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_ingest_records_recipe_health(maker):
    async with maker() as db, api([8, 8, 0, 0, "404"]) as client:
        c = Company(name="Acme", ats="recipe", slug="acme.com", recipe=RECIPE, recipe_state=VALIDATED,
                    recipe_health={"baseline_yield": 8, "last_yield": 8})
        db.add(c)
        await db.commit()
        states = []
        for _ in range(5):
            await ingest_company(db, client, c)
            await db.refresh(c)
            states.append((c.recipe_state, c.recipe_health["last_yield"], c.recipe_health["zero_runs"]))
        # 8 jobs twice; then two empty runs (the empty-response guard keeps the 8 jobs); then a 404
        assert states == [(ACTIVE, 8, 0), (ACTIVE, 8, 0), (ACTIVE, 0, 1), (DEGRADED, 0, 2), (DEGRADED, 0, 3)]
        assert c.recipe_health["last_error"] and c.consecutive_failures == 1  # failure backoff untouched
        active = await db.scalar(sa.select(sa.func.count()).select_from(Job).where(
            Job.company_id == c.id, Job.is_active.is_(True)))
        assert active == 8  # the empty-response guard still applies to recipe boards
        assert needs_reinduce(c)


async def test_ingest_leaves_other_boards_alone(maker):
    async with maker() as db:
        c = Company(name="Gone", ats="nope", slug="gone")
        db.add(c)
        await db.commit()
        await ingest_company(db, None, c)
        await db.refresh(c)
        assert c.recipe_state is None and c.recipe_health is None
