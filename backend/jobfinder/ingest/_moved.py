"""Old import paths for the modules #42 moved out of ingest/ (to integrations/ and services/). #53 deletes them.

`alias` makes the old dotted name the *same module object* as the new one, in sys.modules and as an
attribute of the old parent. A plain re-export would copy names, so a test that monkeypatches
"jobfinder.ingest.sources.workday.asyncio.sleep" would patch a copy the moved code never reads.
"""
import importlib
import pkgutil
import sys
from types import ModuleType


def alias(old: str, new: str) -> ModuleType:
    """Register module `new`, and every module under it when it is a package, under the name `old`."""
    module = importlib.import_module(new)
    _register(old, module)
    for info in pkgutil.walk_packages(getattr(module, "__path__", []), new + "."):
        _register(old + info.name[len(new):], importlib.import_module(info.name))
    return module


def _register(name: str, module: ModuleType) -> None:
    sys.modules[name] = module
    parent_name, _, child = name.rpartition(".")
    if parent_name in sys.modules:  # `from <old parent> import <child>` reads the attribute
        setattr(sys.modules[parent_name], child, module)
