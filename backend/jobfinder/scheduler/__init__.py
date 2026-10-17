"""The scheduler process (#48): `python -m jobfinder.scheduler`, one replica (Deployment, Recreate).

It owns the three things that run on a clock rather than on a message:

  cron.py         time: the cron table, which adds a deduped ledger row per scheduled kind
  relay.py        delivery recovery: re-publishes queued, due rows whose publish was lost (or that
                  only just became due, like retries and staggered planner runs)
  maintenance.py  the reaper (running rows whose worker died go back to queued) and pruning

It holds no state of its own: every row lives in Postgres, so nothing is lost while it's down. Due
rows wait in the ledger until the relay is back and stale running rows wait for the next reap. Cron
runs that fall in the downtime are skipped, not replayed (the job store is in memory), as with the
legacy worker. Two replicas would double the cron, hence exactly one.

It runs only with TASKS_BACKEND=ledger; under legacy the poll-loop worker (jobfinder.worker) does all
of this itself.

Liveness: the relay touches ALIVE_FILE (/tmp/scheduler-alive) on every tick (RELAY_SECONDS), so a
probe can check its age, e.g. `find /tmp/scheduler-alive -mmin -2 | grep -q .`. It is touched even when
the tick failed: it says the process is ticking, not that Postgres and valkey are reachable (a restart
would not fix those; failed ticks log a warning each time). /tmp must be writable (an emptyDir under a
read-only root filesystem).
"""
from pathlib import Path

ALIVE_FILE = Path("/tmp/scheduler-alive")
