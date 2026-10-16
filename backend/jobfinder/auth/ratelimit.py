"""Moved (#42) to jobfinder.services.ratelimit. This old name stays importable until #53, as the same
module object (see ingest/_moved.py)."""
from jobfinder.ingest._moved import alias

alias(__name__, "jobfinder.services.ratelimit")
