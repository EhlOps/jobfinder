"""Shim until #53: moved to jobfinder.integrations.claude.tasks. New code imports from there.

Both names are the same module object (sys.modules), so tests that patch either one patch both.
"""
import sys

from jobfinder.integrations.claude import tasks

sys.modules[__name__] = tasks
