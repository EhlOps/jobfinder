"""Every module imports on its own (#42).

An import cycle fails only when its modules are entered in a particular order, so importing them all into
one process would hide one: the first import loads the rest in a lucky order. So each module is imported
with no other jobfinder module loaded: a separate interpreter drops every `jobfinder*` entry from
sys.modules before each import (third-party packages stay loaded, which keeps this fast), and a cycle
shows up whichever module it is entered from.
"""
import pkgutil
import subprocess
import sys

import pytest

import jobfinder

MODULES = sorted(m.name for m in pkgutil.walk_packages(jobfinder.__path__, "jobfinder."))
# Old paths the #42 shims keep working. walk_packages cannot list them: they are aliases, not files.
OLD_PATHS = [
    "jobfinder.ingest.sources.greenhouse",
    "jobfinder.ingest.sources.custom",
    "jobfinder.ingest.sources.usajobs",
    "jobfinder.ingest.sources.jobspy_source",
    "jobfinder.ingest.recipes.html",
    "jobfinder.ingest.discovery.robots",
    "jobfinder.ingest.discovery.github_lists",
    "jobfinder.auth.ratelimit",
]
NAMES = MODULES + OLD_PATHS
SHARDS = 4  # one interpreter per shard, so xdist can run them side by side

_IMPORT_EACH_ALONE = """
import importlib, sys, traceback
for name in sys.argv[1:]:
    for loaded in [m for m in sys.modules if m == "jobfinder" or m.startswith("jobfinder.")]:
        del sys.modules[loaded]
    try:
        importlib.import_module(name)
    except BaseException:
        print(f"{name}: {traceback.format_exc().strip().splitlines()[-1]}")
"""


def test_walk_finds_the_moved_packages():
    assert {"jobfinder.integrations.ats.registry", "jobfinder.integrations.recipes.validation",
            "jobfinder.integrations.directories", "jobfinder.services.ratelimit"} <= set(MODULES)


@pytest.mark.parametrize("shard", range(SHARDS))
def test_every_module_imports_on_its_own(shard):
    names = NAMES[shard::SHARDS]
    r = subprocess.run([sys.executable, "-c", _IMPORT_EACH_ALONE, *names], capture_output=True, text=True,
                       timeout=300, check=False)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.splitlines() == []


def test_old_paths_are_the_moved_modules():
    """A test that monkeypatches the old path must patch the module the code runs."""
    from jobfinder.auth import ratelimit as old_ratelimit
    from jobfinder.ingest.discovery import robots as old_robots
    from jobfinder.ingest.recipes import html as old_html
    from jobfinder.ingest.sources import fetch_greenhouse
    from jobfinder.ingest.sources import workday as old_workday
    from jobfinder.integrations import robots
    from jobfinder.integrations.ats import workday
    from jobfinder.integrations.ats.greenhouse import fetch_greenhouse as moved_fetch_greenhouse
    from jobfinder.integrations.recipes import html
    from jobfinder.services import ratelimit

    assert (old_workday, old_html, old_robots, old_ratelimit) == (workday, html, robots, ratelimit)
    assert sys.modules["jobfinder.ingest.sources.workday"] is workday
    assert fetch_greenhouse is moved_fetch_greenhouse
