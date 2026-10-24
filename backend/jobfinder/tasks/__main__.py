"""`python -m jobfinder.tasks priority|ai|io|render`: run one queue's taskiq worker (one process).

It imports the queue's handler modules (WORKER_MODULES; @ledger_task registers each kind on its own
queue's broker, so the process consumes only this queue's kinds), runs up to the queue's concurrency
at once (WORKER_AI_CONCURRENCY, WORKER_IO_CONCURRENCY, RENDERER_CONCURRENCY) and acks a message only
after its task has run, so a killed worker's messages are redelivered. A worker for a queue in
PRIORITY_QUEUES asks the Claude gate for permits in the priority lane, every other in bulk.
"""
import signal
import sys

from jobfinder.core import logging as core_logging
from jobfinder.core.config import settings
from jobfinder.integrations.claude import gate
from jobfinder.tasks.brokers import CONCURRENCY

PRIORITY_QUEUES = {"priority"}
# The modules each queue's worker imports: those holding its kinds' @ledger_task handlers. A module may
# register kinds of other queues too; they land on those queues' brokers, which this process never reads.
WORKER_MODULES = {
    "priority": ["jobfinder.tasks.ai", "jobfinder.tasks.documents"],  # #124
    "ai": ["jobfinder.tasks.ai"],
    "io": ["jobfinder.tasks.io"],  # its tasks.documents import lands on the priority broker, unread here
    "render": ["jobfinder.tasks.render"],
}

# On SIGTERM a worker waits this long for its running tasks, then exits. Each stays under its pod's
# terminationGracePeriodSeconds (200s for worker-ai and worker-priority, 60s for the others), so it
# exits cleanly; 180s lets a matching slice (~2.5 minutes) finish when KEDA scales a pod in, instead of
# leaving it to the reaper.
WAIT_TASKS_SECONDS = {"ai": 180, "priority": 180}
DEFAULT_WAIT_TASKS_SECONDS = 50


def worker_argv(queue: str) -> list[str]:
    return [
        f"jobfinder.tasks.brokers:{queue}_broker", *WORKER_MODULES[queue],
        "--workers", "1",
        "--max-async-tasks", str(getattr(settings, CONCURRENCY[queue])),
        "--ack-type", "when_executed",
        "--wait-tasks-timeout", str(WAIT_TASKS_SECONDS.get(queue, DEFAULT_WAIT_TASKS_SECONDS)),
        "--no-configure-logging",
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in CONCURRENCY:
        print(__doc__)
        return 2
    from taskiq.cli.worker.args import WorkerArgs
    from taskiq.cli.worker.run import run_worker

    core_logging.configure(settings.log_level)
    # Set before taskiq starts the worker process, which inherits it: taskiq forks on Linux (the images);
    # under spawn (macOS) the child would start in bulk.
    gate.set_lane("priority" if argv[0] in PRIORITY_QUEUES else "bulk")
    # The stack-dump signals (tasks/diagnostics.py, #136) are for the worker child, which installs its own
    # handlers at WORKER_STARTUP; sent to this process by mistake, they would kill the pod and its evidence.
    for sig in (signal.SIGUSR1, signal.SIGUSR2):
        signal.signal(sig, signal.SIG_IGN)
    return run_worker(WorkerArgs.from_cli(worker_argv(argv[0]))) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
