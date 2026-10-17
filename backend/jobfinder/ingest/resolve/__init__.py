"""Moved (#46) to jobfinder.services.resolver (with .ladder, .names and .rungs). These old names stay
importable until #53, as the same module objects (see ingest/_moved.py)."""
from jobfinder.ingest._moved import alias

alias(__name__, "jobfinder.services.resolver")
