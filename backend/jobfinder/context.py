"""The app context: core's infrastructure plus the integrations, `ctx.claude` and `ctx.store`.

core/context.py builds what every process shares (engine, sessions, HTTP client, clock) but may not
import anything outside core, so the integrations are added here, one layer up, on the same objects:

    ctx.claude   AITasks over the Claude CLI client; every call is recorded in ai_calls
    ctx.store    the document store (uploads, fetched links, tailored resumes)

`default_context()` is the process's own (the api, the worker and the CLI), built on first use from
core's. Tests build theirs and pass fakes: `build_context(settings, claude=AITasks(FakeClaude()))`.
"""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from jobfinder.core import context as core
from jobfinder.core.config import Settings
from jobfinder.core.db import Sessions
from jobfinder.integrations.claude.claude_code import CallRecord, ClaudeCode, Recorder
from jobfinder.integrations.claude.tasks import AITasks
from jobfinder.models import AICall
from jobfinder.storage.documents import DocumentStore


@dataclass
class AppContext(core.AppContext):
    claude: AITasks
    store: DocumentStore


def record_calls(sessions: Sessions) -> Recorder:
    """A recorder that writes each Claude call (task, model, timing, tokens) to ai_calls."""

    async def record(rec: CallRecord) -> None:
        async with sessions() as db:
            db.add(AICall(**rec.__dict__))
            await db.commit()

    return record


def with_integrations(
    base: core.AppContext, *, claude: AITasks | None = None, store: DocumentStore | None = None
) -> AppContext:
    """`base` plus the integrations; what is not passed in is built from its settings."""
    return AppContext(
        **vars(base),
        claude=claude or AITasks(ClaudeCode(recorder=record_calls(base.sessions))),
        store=store or DocumentStore(Path(base.settings.store_dir)),
    )


def build_context(
    settings: Settings, *, claude: AITasks | None = None, store: DocumentStore | None = None, **infra
) -> AppContext:
    """core.context.build_context (`infra`: engine, sessions, http, clock), then the integrations."""
    return with_integrations(core.build_context(settings, **infra), claude=claude, store=store)


@lru_cache
def default_context() -> AppContext:
    """This process's context: core's default one, so `jobfinder.db` shares its engine and sessions."""
    return with_integrations(core.default_context())
