"""Moved (#42): board discovery is jobfinder.integrations.directories, and its robots.txt-aware fetcher is
jobfinder.integrations.robots. These old names stay importable until #53, as the same module objects
(see ingest/_moved.py)."""
from jobfinder.ingest._moved import alias

alias(__name__, "jobfinder.integrations.directories")
alias(f"{__name__}.robots", "jobfinder.integrations.robots")  # sets directories.robots too: the same module
