"""The task platform (#48): Postgres is the ledger, valkey only delivers.

  kinds.py    kind -> queue (the stream that delivers it: ai, io, render)
  ledger.py   every write to the tasks table: add (flushes into the caller's transaction), claim,
              finish, fail, heartbeat, requeue, reap, and the relay's queries
  brokers.py  the three taskiq valkey-stream brokers and `publish`
  runtime.py  @ledger_task: claim -> validate -> run -> finish, for the worker processes
  __main__    `python -m jobfinder.tasks ai|io|render` runs one queue's worker

Rows added with TASKS_BACKEND=ledger are published after their transaction commits; anything lost on
the way is re-sent by the scheduler's relay (jobfinder.scheduler). With TASKS_BACKEND=legacy nothing is
published and the poll-loop worker (jobfinder.worker) claims rows straight from the table.
"""
