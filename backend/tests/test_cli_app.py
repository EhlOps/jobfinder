"""The CLI as a whole: every verb has help, errors map to exit codes."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jobfinder.cli import app, ops
from jobfinder.core.errors import Invalid
from jobfinder.integrations.claude.claude_code import AIError
from jobfinder.models import User
from tests.cli_runner import invoke, invoke_sync

VERBS = [
    ["users", "add"], ["users", "list"], ["users", "reset-link"], ["users", "admin", "grant"],
    ["users", "admin", "revoke"], ["boards", "add"], ["boards", "list"], ["boards", "set"],
    ["ingest", "run"], ["digest", "send"], ["questions", "collect"], ["email", "test"],
    ["ai", "check"], ["tasks", "list"], ["tasks", "retry"],
]


def test_every_group_and_verb_has_help():
    root = invoke_sync("--help")
    assert root.exit_code == 0
    for group in {v[0] for v in VERBS}:
        assert group in root.stdout
    for verb in VERBS:
        r = invoke_sync(*verb, "--help")
        assert r.exit_code == 0, (verb, r.output)
        assert f"jobfinder {' '.join(verb)}" in r.stdout


@pytest.mark.real_commit
async def test_python_m_runs_the_app(maker):
    """`python -m jobfinder.cli` is the Typer app; the pre-#49 flat names are gone (#53). conftest points
    DATABASE_URL at this database."""
    async with maker() as db:
        db.add(User(email="old@x.com"))
        await db.commit()

    def python_m(*argv):
        return subprocess.run(
            [sys.executable, "-m", "jobfinder.cli", *argv], capture_output=True, text=True,
            cwd=Path(__file__).resolve().parents[1], env=os.environ, timeout=60, check=False,
        )

    p = await asyncio.to_thread(python_m, "users", "list")
    assert p.returncode == 0, p.stderr
    assert "old@x.com" in p.stdout
    assert (await asyncio.to_thread(python_m, "list-users")).returncode == 2


def test_an_app_error_exits_1(monkeypatch):
    async def boom():
        raise Invalid("that is not right")

    monkeypatch.setattr(ops, "_ai_check", boom)
    r = invoke_sync("ai", "check")
    assert r.exit_code == 1 and "that is not right" in r.stderr


async def test_ai_check_failure_exits_1(monkeypatch):
    from jobfinder.context import default_context

    async def ping():
        raise AIError("auth", "no token")

    monkeypatch.setattr(default_context().claude, "ping", ping)
    r = await invoke("ai", "check")
    assert r.exit_code == 1 and "AI check FAILED" in r.stderr and "setup-token" in r.stderr


@pytest.mark.real_commit
async def test_digest_and_questions_with_nobody_to_do(maker):
    r = await invoke("digest", "send")
    assert r.exit_code == 0 and "'due': 0" in r.stdout
    assert (await invoke("questions", "collect")).exit_code == 0
    r = await invoke("digest", "send", "--email", "no@x.com")
    assert r.exit_code == 1 and "No user no@x.com" in r.stderr


def test_usage_errors_exit_2():
    assert invoke_sync("boards", "set").exit_code == 2
    assert invoke_sync("digest", "send", "--preview").exit_code == 2
    assert invoke_sync("nope").exit_code == 2
    assert app  # the script's Typer root
