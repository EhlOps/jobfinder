"""Shim until #53: moved to jobfinder.integrations.smtp. New code imports from there.

Both names are the same module object (sys.modules), so tests that patch either one patch both.
"""
import sys

from jobfinder.integrations import smtp

sys.modules[__name__] = smtp
