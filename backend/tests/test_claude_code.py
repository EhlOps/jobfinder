import asyncio

import pytest
from pydantic import BaseModel
from stub_claude import StubClaude, envelope

from jobfinder.ai.claude_code import AIError, CallRecord, ClaudeCode


class Greeting(BaseModel):
    greeting: str
    n: int


def make(tmp_path, steps, **kw):
    stub = StubClaude(tmp_path, steps)
    return stub, ClaudeCode(claude_bin=str(stub.path), scratch_dir=str(tmp_path), oauth_token="tok-123", **kw)


async def test_structured_output_and_locked_down_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    stub, ai = make(tmp_path, [{"stdout": envelope(structured_output={"greeting": "hi", "n": 3})}])
    out = await ai.run("SECRET PROMPT", system="be terse", output=Greeting, model="haiku")
    assert out == Greeting(greeting="hi", n=3)

    call = stub.calls[0]
    argv = call["argv"]
    assert call["stdin"] == "SECRET PROMPT" and "SECRET PROMPT" not in " ".join(argv)  # prompt via stdin only
    for flag in ("-p", "--restricted", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--system-prompt") + 1] == "be terse"
    assert "greeting" in argv[argv.index("--json-schema") + 1]
    assert "--bare" not in argv
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-123"


async def test_falls_back_to_json_in_result_text(tmp_path):
    _, ai = make(tmp_path, [{"stdout": envelope(result='```json\n{"greeting": "yo", "n": 1}\n```')}])
    assert (await ai.run("p", system="s", output=Greeting)).greeting == "yo"


async def test_plain_text_when_no_schema(tmp_path):
    _, ai = make(tmp_path, [{"stdout": envelope(result="  Dear team,  ")}])
    assert await ai.run("p", system="s") == "Dear team,"


async def test_repair_retry_then_success(tmp_path):
    stub, ai = make(
        tmp_path,
        [
            {"stdout": envelope(result="not json at all")},
            {"stdout": envelope(structured_output={"greeting": "ok", "n": 2})},
        ],
    )
    assert (await ai.run("p", system="s", output=Greeting)).n == 2
    assert len(stub.calls) == 2 and "invalid" in stub.calls[1]["stdin"]


async def test_bad_output_after_repair_raises(tmp_path):
    stub, ai = make(tmp_path, [{"stdout": envelope(result="nope")}, {"stdout": envelope(result="still nope")}])
    with pytest.raises(AIError) as e:
        await ai.run("p", system="s", output=Greeting)
    assert e.value.kind == "bad_output" and not e.value.retryable
    assert len(stub.calls) == 2


@pytest.mark.parametrize(
    ("result", "kind", "retryable"),
    [
        ("Not logged in · Please run /login", "auth", False),
        ("Failed to authenticate. API Error: 401 OAuth access token is invalid.", "auth", False),
        ("API Error: 401 Unauthorized", "auth", False),
        ("Claude usage limit reached. Resets at 5pm", "rate_limited", True),
        ("something exploded", "unknown", False),
    ],
)
async def test_error_classification(tmp_path, result, kind, retryable):
    _, ai = make(tmp_path, [{"stdout": envelope(is_error=True, result=result)}])
    with pytest.raises(AIError) as e:
        await ai.run("p", system="s")
    assert e.value.kind == kind and e.value.retryable is retryable


async def test_non_json_stdout_is_classified_from_stderr(tmp_path):
    _, ai = make(tmp_path, [{"stdout": "", "stderr": "429 Too Many Requests", "exit": 1}])
    with pytest.raises(AIError) as e:
        await ai.run("p", system="s")
    assert e.value.kind == "rate_limited"


async def test_timeout_kills_and_raises(tmp_path, monkeypatch):
    procs = []
    real = asyncio.create_subprocess_exec

    async def spy(*a, **k):
        procs.append(await real(*a, **k))
        return procs[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    _, ai = make(tmp_path, [{"sleep": 5, "stdout": envelope(result="late")}])
    with pytest.raises(AIError) as e:
        await ai.run("p", system="s", timeout_s=0.5)
    assert e.value.kind == "timeout" and e.value.retryable
    assert procs[0].returncode is not None  # killed and reaped, not left as a zombie


async def test_missing_binary(tmp_path):
    ai = ClaudeCode(claude_bin=str(tmp_path / "nope"), scratch_dir=str(tmp_path))
    with pytest.raises(AIError) as e:
        await ai.run("p", system="s")
    assert e.value.kind == "cli_missing"


async def test_recorder_called_for_success_and_failure(tmp_path):
    records: list[CallRecord] = []

    async def rec(r: CallRecord):
        records.append(r)

    _, ai = make(
        tmp_path,
        [{"stdout": envelope(result="ok")}, {"stdout": envelope(is_error=True, result="usage limit reached")}],
        recorder=rec,
    )
    await ai.run("p", system="s", task="t1")
    with pytest.raises(AIError):
        await ai.run("p", system="s", task="t2")
    assert [(r.task, r.ok, r.error_kind) for r in records] == [("t1", True, None), ("t2", False, "rate_limited")]
    assert records[0].input_tokens == 10 and records[0].output_tokens == 5
