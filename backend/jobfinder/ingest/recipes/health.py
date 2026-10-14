"""Recipe health: the lifecycle of an ats='recipe' board (.plans/2026-10-13-adaptive-job-scraping).

    induced -> validated -> active <-> degraded -> (re-induce) -> validated | disabled

State lives on Company.recipe_state; yields and history on Company.recipe_health:
    baseline_yield     what a healthy run returns (see _baseline)
    last_yield         jobs on the last run
    zero_runs          consecutive runs that returned nothing or failed
    degraded_since     ISO time the recipe went degraded (None otherwise)
    reinduce_count_7d  re-inductions in the window starting at reinduce_window_start (ISO)
    last_error         the last run's error, if any

Rules:
  * a healthy run (jobs, and not a big drop) makes induced/validated/degraded -> active;
  * 0 jobs (or an error: a redesigned site usually 404s or stops returning JSON) twice in a row, or a yield
    more than 60% below baseline_yield, makes the recipe degraded;
  * a degraded recipe is re-induced (by the LLM unit) at most REINDUCE_CAP times per 7 days:
    record_reinduce(ok=True) stores the new recipe as validated; ok=False disables the board for an admin.

These are pure functions on the ORM object; the caller commits. Note the error/backoff rules in
ingest.service (consecutive_failures -> auto-disable) still apply to recipe boards as well.
"""
from datetime import UTC, datetime, timedelta

from jobfinder.models import Company

INDUCED, VALIDATED, ACTIVE, DEGRADED, DISABLED = "induced", "validated", "active", "degraded", "disabled"
ZERO_RUNS_TO_DEGRADE = 2
DROP_RATIO = 0.4  # a yield below 40% of baseline (a >60% drop) is degraded
BASELINE_DECAY = 0.8  # EMA weight kept on the old baseline
REINDUCE_CAP = 2
REINDUCE_WINDOW = timedelta(days=7)


def _baseline(old: int | None, n: int) -> int:
    """Baseline = max(this healthy yield, EMA of healthy yields). It jumps up to a new high at once (so a
    big board that briefly halves trips the 60% rule) and drifts down slowly when a board really shrinks,
    instead of a rolling max that would hold a stale peak forever. Only healthy runs update it."""
    if not old:
        return n
    return max(n, round(BASELINE_DECAY * old + (1 - BASELINE_DECAY) * n))


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _parse(ts) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


def record_run(company: Company, n_postings: int, error: str | None = None, *,
               now: datetime | None = None) -> str:
    """Fold one ingest run into the recipe's health; returns the new state."""
    h = dict(company.recipe_health or {})
    state = company.recipe_state or VALIDATED
    h["last_yield"] = n_postings
    h["last_error"] = error[:300] if error else None
    if state == DISABLED:
        company.recipe_health = h
        return state
    baseline = h.get("baseline_yield") or 0
    if error or n_postings <= 0:
        h["zero_runs"] = (h.get("zero_runs") or 0) + 1
        bad = h["zero_runs"] >= ZERO_RUNS_TO_DEGRADE
    else:
        h["zero_runs"] = 0
        bad = baseline > 0 and n_postings < DROP_RATIO * baseline
        if not bad:
            h["baseline_yield"] = _baseline(baseline, n_postings)
    if bad:
        if state != DEGRADED:
            h["degraded_since"] = _now(now).isoformat()
        state = DEGRADED
    elif not error and n_postings > 0:
        state, h["degraded_since"] = ACTIVE, None
    # else: a first empty/failed run keeps the state; one more makes it degraded
    company.recipe_state = state
    company.recipe_health = h  # reassign: JSON columns don't track in-place changes
    return state


def _window(h: dict, now: datetime) -> int:
    """Re-inductions in the current 7-day window (resets the window once it has passed)."""
    start = _parse(h.get("reinduce_window_start"))
    if start is None or now - start >= REINDUCE_WINDOW:
        h["reinduce_window_start"], h["reinduce_count_7d"] = now.isoformat(), 0
    return h.get("reinduce_count_7d") or 0


def needs_reinduce(company: Company, *, now: datetime | None = None) -> bool:
    """A degraded recipe board that hasn't used up its re-inductions this week. Enabled, or auto-disabled
    by its failure streak (a redesigned site that 404s gets there quickly)."""
    if company.recipe_state != DEGRADED:
        return False
    if not company.enabled and not (company.disabled_reason or "").startswith("auto-disabled"):
        return False  # switched off by an admin (a failure-streak auto-disable can still heal)
    return _window(dict(company.recipe_health or {}), _now(now)) < REINDUCE_CAP


def record_reinduce(company: Company, ok: bool, new_recipe: dict | None, *,
                    now: datetime | None = None) -> str:
    """Record a re-induction attempt. Success (ok and a new recipe) stores the recipe as validated with a
    fresh baseline; the next healthy run makes it active. Failure disables the board with a reason (the
    caller should also deactivate its jobs, as ingest.service._fail_board does on auto-disable)."""
    now = _now(now)
    h = dict(company.recipe_health or {})
    h["reinduce_count_7d"] = _window(h, now) + 1
    if ok and new_recipe:
        company.recipe = new_recipe
        company.recipe_state = VALIDATED
        company.enabled, company.disabled_reason = True, None  # a fresh start for the failure streak too
        company.consecutive_failures, company.last_error = 0, None
        h.update(baseline_yield=None, zero_runs=0, degraded_since=None, last_error=None)
    else:
        company.recipe_state = DISABLED
        company.enabled = False
        why = h.get("last_error") or f"yield {h.get('last_yield', 0)} vs baseline {h.get('baseline_yield')}"
        company.disabled_reason = f"recipe re-induction failed after the recipe degraded ({why})"[:500]
    company.recipe_health = h
    return company.recipe_state
