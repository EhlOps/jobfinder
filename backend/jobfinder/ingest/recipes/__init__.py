"""Moved (#42) to jobfinder.integrations.recipes. This old name stays importable until #53, as the same
module objects (see ingest/_moved.py)."""
from jobfinder.ingest._moved import alias

alias(__name__, "jobfinder.integrations.recipes")
