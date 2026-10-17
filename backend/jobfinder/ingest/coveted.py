"""Moved (#46) to jobfinder.services.coverage. This old name stays importable until #53, as the same
module object (see ingest/_moved.py)."""
from jobfinder.ingest._moved import alias

alias(__name__, "jobfinder.services.coverage")
