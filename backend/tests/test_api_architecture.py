"""The rules of the api/schemas split: routers hold routing code only, and every request and response body
lives in jobfinder/schemas, which never borrows an AI output schema as the API contract."""
import ast
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest
from pydantic import BaseModel

import jobfinder.api
from jobfinder.ai.schemas import TailoredResume
from jobfinder.schemas.resumes import TailoredResumeOut

PKG = Path(__file__).resolve().parents[1] / "jobfinder"
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
def test_schemas_are_documented_and_do_not_import_routers_or_ai(path):
    tree = ast.parse(path.read_text())
    assert ast.get_docstring(tree), f"{path.name} needs a module docstring"
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path.name}: use absolute imports"
            names = [node.module or ""]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        for name in names:
            assert name.split(".")[:2] not in (["jobfinder", "api"], ["jobfinder", "ai"]), f"{path.name} imports {name}"


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
