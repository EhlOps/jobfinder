"""Shim until #53: moved to jobfinder.integrations.claude.claude_code. New code imports from there.

Both names are the same module object (sys.modules), so tests that patch either one patch both.
"""
import sys

from jobfinder.integrations.claude import claude_code

sys.modules[__name__] = claude_code
