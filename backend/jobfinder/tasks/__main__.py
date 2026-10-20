"""`python -m jobfinder.tasks ai|io|render`: run one queue's taskiq worker (one process).

It imports `jobfinder.tasks.<queue>` (where that queue's @ledger_task handlers live), runs up to the
queue's concurrency at once (AI_MAX_CONCURRENCY, WORKER_IO_CONCURRENCY, RENDERER_CONCURRENCY) and
acks a message only after its task has run, so a killed worker's messages are redelivered.
"""
import sys

from jobfinder.core import logging as core_logging
from jobfinder.core.config import settings
from jobfinder.tasks.brokers import CONCURRENCY


def worker_argv(queue: str) -> list[str]:
    return [
        f"jobfinder.tasks.brokers:{queue}_broker", f"jobfinder.tasks.{queue}",
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
    return run_worker(WorkerArgs.from_cli(worker_argv(argv[0]))) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
