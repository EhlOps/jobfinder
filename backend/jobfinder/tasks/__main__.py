"""`python -m jobfinder.tasks ai|io|render`: run one queue's taskiq worker (one process).

It imports the queue's handler modules (WORKER_MODULES; @ledger_task registers each kind on its own
queue's broker, so the process consumes only this queue's kinds), runs up to the queue's concurrency
at once (WORKER_AI_CONCURRENCY, WORKER_IO_CONCURRENCY, RENDERER_CONCURRENCY) and acks a message only
after its task has run, so a killed worker's messages are redelivered. A worker for a queue in
PRIORITY_QUEUES asks the Claude gate for permits in the priority lane, every other in bulk.
"""
import sys

from jobfinder.core import logging as core_logging
from jobfinder.core.config import settings
from jobfinder.integrations.claude import gate
from jobfinder.tasks.brokers import CONCURRENCY

PRIORITY_QUEUES = {"priority"}
# The modules each queue's worker imports: those holding its kinds' @ledger_task handlers. A module may
# register kinds of other queues too; they land on those queues' brokers, which this process never reads.
WORKER_MODULES = {
    "ai": ["jobfinder.tasks.ai"],
    "io": ["jobfinder.tasks.io"],  # which imports tasks.documents (parse_document, fetch_link)
    "render": ["jobfinder.tasks.render"],
}


def worker_argv(queue: str) -> list[str]:
    return [
        f"jobfinder.tasks.brokers:{queue}_broker", *WORKER_MODULES[queue],
        "--workers", "1",
        "--max-async-tasks", str(getattr(settings, CONCURRENCY[queue])),
        "--ack-type", "when_executed",
        # On SIGTERM, wait this long for running tasks: under the pods' 60s grace, so they exit cleanly.
        "--wait-tasks-timeout", "50",
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
    return run_worker(WorkerArgs.from_cli(worker_argv(argv[0]))) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
