"""Process logging: one call at the start of an entrypoint (worker, renderer) sets format and level.

The level comes from LOG_LEVEL. Modules keep using `logging.getLogger("jobfinder.<area>")`.
"""
import logging

FORMAT = logging.BASIC_FORMAT  # "LEVEL:logger:message", what the worker has always logged


def configure(level: str) -> None:
    """Configure the root logger once; `level` is a name such as "INFO" (any case)."""
    logging.basicConfig(level=level.upper(), format=FORMAT)
