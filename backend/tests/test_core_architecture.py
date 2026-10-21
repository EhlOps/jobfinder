"""The rules of jobfinder/core (its __init__ docstring): small files that import nothing else from the app,
and settings that are read through core only."""
import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "jobfinder"
CORE = sorted((PKG / "core").glob("*.py"))
MAX_LINES = 150
MAX_LINES_CONFIG = 160  # config.py's Settings is the one list of every key; it grows with them


def test_core_has_the_ten_files_and_the_rules():
    names = {p.stem for p in CORE} - {"__init__"}
    assert names == {"config", "db", "errors", "http", "clock", "logging", "security", "context", "urls", "text"}


@pytest.mark.parametrize("path", CORE, ids=lambda p: p.name)
def test_core_file_is_small_and_documented(path):
    lines, budget = path.read_text().count("\n"), MAX_LINES_CONFIG if path.name == "config.py" else MAX_LINES
    assert lines <= budget, f"{path.name} has {lines} lines (budget {budget})"
    assert ast.get_docstring(ast.parse(path.read_text())), f"{path.name} needs a module docstring"


@pytest.mark.parametrize("path", CORE, ids=lambda p: p.name)
def test_core_imports_nothing_from_the_app(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        for name in names:
            if name == "jobfinder" or name.startswith("jobfinder."):
                assert name.startswith("jobfinder.core"), f"{path.name} imports {name}"


def test_only_core_mentions_get_settings():
    users = [
        str(p.relative_to(PKG)) for p in PKG.rglob("*.py")
        if "get_settings" in p.read_text() and PKG / "core" not in p.parents
    ]
    assert users == [], "read settings via `from jobfinder.core.config import settings`"
