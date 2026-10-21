"""The architecture rules import-linter can't state (its contracts are in pyproject.toml [tool.importlinter]).

  commits     only the boundaries commit: the request (core/db.py get_db), the CLI, the task handlers,
              the scheduler, plus a few named exceptions. Services and routers never do.
  core        small, documented files that import nothing else from the app; settings are read through it
  api/        routing code only: every request and response body lives in jobfinder/schemas
"""
import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest
from pydantic import BaseModel

import jobfinder.api
from jobfinder.integrations.claude.schemas import TailoredResume
from jobfinder.schemas.resumes import TailoredResumeOut

PKG = Path(__file__).resolve().parents[1] / "jobfinder"
FILES = sorted(PKG.rglob("*.py"))


def rel(path: Path) -> str:
    return path.relative_to(PKG).as_posix()


# ── commits ──────────────────────────────────────────────────────────────
# Where `.commit` may appear (called, or handed on as a checkpoint like `checkpoint=db.commit`).
COMMIT_BOUNDARIES = ("core/db.py", "cli/", "scheduler/", "tasks/runtime.py", "tasks/ai.py", "tasks/io.py",
                     "tasks/documents.py", "tasks/render.py")
COMMIT_EXCEPTIONS = {
    "tasks/ledger.py": "_mark_published records a publish in its own short session",
    "context.py": "the AI-call recorder writes each call in its own session, whatever the caller does",
    "integrations/render/__init__.py": "request_render commits its task so the renderer can claim it while it waits",
    "services/ingestion.py": "a run keeps each board's outcome as it goes (per-board checkpoint)",
    "services/digest.py": "each user's email is its own boundary, so one crash rolls back only theirs",
    "services/daily.py": "a user's matches and questions are marked sent, or given back when the send fails",
    "services/planner.py": "the planner tick keeps its career-stage syncs and its plan",
}


def commit_sites(path: Path) -> list[int]:
    return [n.lineno for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Attribute) and n.attr == "commit"]


def commit_allowed(name: str) -> bool:
    return name.startswith(COMMIT_BOUNDARIES) or name in COMMIT_EXCEPTIONS


def test_only_the_boundaries_commit():
    offenders = [f"{rel(p)}:{line}" for p in FILES if not commit_allowed(rel(p)) for line in commit_sites(p)]
    assert offenders == [], (
        "services, routers and repositories never commit: the request (get_db), the task handler or the CLI "
        f"command does. Return, or take a `checkpoint` callable, instead: {offenders}"
    )


def test_the_commit_allowlist_is_current():
    """Every named exception exists and still commits, so the list can't quietly outlive its reason."""
    stale = [name for name in COMMIT_EXCEPTIONS if not (PKG / name).is_file() or not commit_sites(PKG / name)]
    assert stale == []


@pytest.mark.parametrize("name", ["services/profile.py", "api/profile.py", "repositories/users.py"])
def test_a_commit_anywhere_else_is_refused(name):
    assert not commit_allowed(name)


# ── core ─────────────────────────────────────────────────────────────────
CORE = sorted((PKG / "core").glob("*.py"))
MAX_LINES = 150
LINE_BUDGETS = {"config.py": 160}  # Settings is the one list of every key; it grows with them


def test_core_has_the_ten_files():
    names = {p.stem for p in CORE} - {"__init__"}
    assert names == {"config", "db", "errors", "http", "clock", "logging", "security", "context", "urls", "text"}


@pytest.mark.parametrize("path", CORE, ids=lambda p: p.name)
def test_core_file_is_small_and_documented(path):
    lines, budget = path.read_text().count("\n"), LINE_BUDGETS.get(path.name, MAX_LINES)
    assert lines <= budget, f"core/{path.name} has {lines} lines (budget {budget})"
    assert ast.get_docstring(ast.parse(path.read_text())), f"core/{path.name} needs a module docstring"


def imported_names(path: Path) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            out.append("." * node.level + (node.module or ""))
        elif isinstance(node, ast.Import):
            out += [a.name for a in node.names]
    return out


@pytest.mark.parametrize("path", CORE, ids=lambda p: p.name)
def test_core_imports_nothing_from_the_app(path):
    for name in imported_names(path):  # a relative import from a file in core/ stays in core/
        if name == "jobfinder" or name.startswith("jobfinder."):
            assert name == "jobfinder.core" or name.startswith("jobfinder.core."), f"core/{path.name} imports {name}"


def test_only_core_reads_get_settings():
    readers = [rel(p) for p in FILES if "get_settings" in p.read_text() and not rel(p).startswith("core/")]
    assert readers == [], "read settings via `from jobfinder.core.config import settings`"


# ── api and schemas ──────────────────────────────────────────────────────
API_MODULES = sorted(m.name for m in pkgutil.iter_modules(jobfinder.api.__path__, "jobfinder.api."))
SCHEMA_FILES = sorted((PKG / "schemas").glob("*.py"))


@pytest.mark.parametrize("name", API_MODULES)
def test_no_pydantic_model_is_defined_in_api(name):
    module = importlib.import_module(name)
    defined = [
        n for n, obj in vars(module).items()
        if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj.__module__ == name
    ]
    assert defined == [], f"move {defined} to jobfinder/schemas/{name.rsplit('.', 1)[1]}.py"


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=lambda p: p.name)
def test_schemas_are_documented_and_use_absolute_imports(path):
    """What they may import is the layers contract's job; this keeps them readable on their own."""
    assert ast.get_docstring(ast.parse(path.read_text())), f"schemas/{path.name} needs a module docstring"
    assert not [n for n in imported_names(path) if n.startswith(".")], f"schemas/{path.name}: use absolute imports"


def _untitled(schema):
    """A JSON schema without its titles: a title is the class or field name, not part of the shape."""
    if isinstance(schema, dict):
        return {k: _untitled(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_untitled(v) for v in schema]
    return schema


def test_tailored_resume_out_matches_the_ai_schema():
    """The resume endpoints read files the AI wrote as TailoredResume, so the two must stay field-for-field equal."""
    assert _untitled(TailoredResumeOut.model_json_schema()) == _untitled(TailoredResume.model_json_schema())



def test_services_build_http_clients_only_through_core_http():
    """core.http.new_client sends the User-Agent every fetch must carry; an inline AsyncClient forgets it."""
    inline = [f"{rel(p)}:{n.lineno}" for p in FILES if rel(p).startswith("services/")
              for n in ast.walk(ast.parse(p.read_text()))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "AsyncClient"]
    assert inline == []
