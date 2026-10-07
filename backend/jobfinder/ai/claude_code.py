"""Runs the Claude Code CLI headlessly (`claude -p`) so AI calls use a Claude subscription.

Safety: the model gets no tools (`--tools ""`) and ignores user/project settings
(`--restricted`), so text injected via scraped job postings or resumes can't reach files,
a shell, or the network. The prompt goes over stdin, never argv. The child gets a minimal
environment: ANTHROPIC_API_KEY is deliberately excluded so the CLI can't switch to API
billing, and `--bare` is never used because it forces API-key auth.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TypeVar, overload

from pydantic import BaseModel, ValidationError

from jobfinder.ai import credentials
from jobfinder.config import get_settings

T = TypeVar("T", bound=BaseModel)
Model = Literal["haiku", "sonnet", "opus"]
ErrorKind = Literal["auth", "rate_limited", "timeout", "bad_output", "cli_missing", "unknown"]

# Keychain-backed login on macOS needs USER/LOGNAME/TMPDIR as well as HOME.
_ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "CLAUDE_CONFIG_DIR")

_AUTH_RE = re.compile(
    r"not logged in|please run /login|authenticat|unauthorized|\b401\b|"
    r"invalid (?:api key|token)|oauth (?:access )?token",
    re.IGNORECASE,
)
_RATE_RE = re.compile(
    r"usage limit|rate.?limit|limit reached|too many requests|overloaded|\b429\b|\b529\b", re.IGNORECASE
)


class AIError(Exception):
    def __init__(self, kind: ErrorKind, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.kind in ("rate_limited", "timeout")


@dataclass
class CallRecord:
    task: str
    model: str
    duration_ms: int
    ok: bool
    error_kind: str | None
    input_tokens: int
    output_tokens: int


Recorder = Callable[[CallRecord], Awaitable[None]]


def _classify(text: str) -> ErrorKind:
    if _AUTH_RE.search(text):
        return "auth"
    if _RATE_RE.search(text):
        return "rate_limited"
    return "unknown"


def _extract_json(text: str) -> object:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end > start:
            return json.loads(s[start : end + 1])
        raise


class ClaudeCode:
    def __init__(
        self,
        *,
        claude_bin: str | None = None,
        scratch_dir: str | None = None,
        oauth_token: str | None = None,
        max_concurrency: int | None = None,
        recorder: Recorder | None = None,
    ):
        s = get_settings()
        self._bin = claude_bin or s.claude_bin
        self._scratch = scratch_dir or s.claude_scratch_dir
        self._token = s.claude_code_oauth_token if oauth_token is None else oauth_token
        self._sem = asyncio.Semaphore(max_concurrency or s.ai_max_concurrency)
        self._recorder = recorder

    def _env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
        # Token saved from the admin page wins (re-read every call, so changes apply at once),
        # then the env fallback; with neither, the CLI uses its own login if it has one.
        token = credentials.read_token() or self._token
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env

    def _argv(self, system: str, model: str, schema: dict | None) -> list[str]:
        argv = [
            self._bin, "-p",
            "--output-format", "json",
            "--model", model,
            "--system-prompt", system,
            "--tools", "",
            "--restricted",
            "--permission-prompts", "none",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        return argv

    async def _invoke(self, argv: list[str], prompt: str, timeout_s: float) -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._scratch,
                env=self._env(),
                start_new_session=True,  # own process group so a timeout kills children too
            )
        except FileNotFoundError:
            raise AIError("cli_missing", f"`{self._bin}` not found on PATH") from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout_s)
        except (TimeoutError, asyncio.CancelledError) as e:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(proc.wait()), 5)  # reap it so no zombie is left behind
            if isinstance(e, asyncio.CancelledError):
                raise
            raise AIError("timeout", f"claude exceeded {timeout_s}s") from None

        stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            raise AIError(_classify(stderr + stdout), (stderr or stdout)[:500]) from None
        if envelope.get("is_error") or envelope.get("type") != "result":
            msg = str(envelope.get("result") or stderr or "claude failed")
            raise AIError(_classify(msg + stderr), msg[:500])
        return envelope

    @overload
    async def run(self, prompt: str, *, system: str, output: type[T], model: Model = ..., timeout_s: float = ..., task: str = ...) -> T: ...
    @overload
    async def run(self, prompt: str, *, system: str, output: None = ..., model: Model = ..., timeout_s: float = ..., task: str = ...) -> str: ...

    async def run(
        self,
        prompt: str,
        *,
        system: str,
        output: type[BaseModel] | None = None,
        model: Model = "sonnet",
        timeout_s: float = 180,
        task: str = "",
    ):
        schema = output.model_json_schema() if output else None
        argv = self._argv(system, model, schema)
        t0 = time.monotonic()
        usage: dict = {}
        error_kind: str | None = None
        try:
            async with self._sem:
                envelope = await self._invoke(argv, prompt, timeout_s)
                usage = envelope.get("usage") or {}
                if output is None:
                    return str(envelope.get("result", "")).strip()
                try:
                    return self._validate(output, envelope)
                except (ValidationError, ValueError, json.JSONDecodeError) as first:
                    # One repair attempt, feeding the failure back to the model.
                    repair = (
                        f"{prompt}\n\nYour previous answer was invalid ({str(first)[:600]}). "
                        "Reply again with ONLY valid JSON matching the schema."
                    )
                    envelope = await self._invoke(argv, repair, timeout_s)
                    try:
                        return self._validate(output, envelope)
                    except (ValidationError, ValueError, json.JSONDecodeError) as e:
                        raise AIError("bad_output", str(e)[:500]) from None
        except AIError as e:
            error_kind = e.kind
            raise
        finally:
            if self._recorder:
                with contextlib.suppress(Exception):  # logging must never break a call
                    await self._recorder(
                        CallRecord(
                            task=task,
                            model=model,
                            duration_ms=int((time.monotonic() - t0) * 1000),
                            ok=error_kind is None,
                            error_kind=error_kind,
                            input_tokens=int(usage.get("input_tokens", 0) or 0),
                            output_tokens=int(usage.get("output_tokens", 0) or 0),
                        )
                    )

    @staticmethod
    def _validate(output: type[T], envelope: dict) -> T:
        data = envelope.get("structured_output")
        if data is None:
            data = _extract_json(str(envelope.get("result", "")))
        return output.model_validate(data)
