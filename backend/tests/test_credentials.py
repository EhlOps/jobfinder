import logging
import os
import stat

import pytest
from stub_claude import StubClaude, envelope

from jobfinder.ai import credentials
from jobfinder.ai.claude_code import ClaudeCode
from jobfinder.config import get_settings

TOKEN = "sk-ant-oat01-" + "x" * 40


def test_round_trip_permissions_and_delete():
    assert credentials.read_token() is None and credentials.source() == "none"
    credentials.write_token(f"  {TOKEN}\n")
    assert credentials.read_token() == TOKEN and credentials.source() == "file"
    mode = stat.S_IMODE(os.stat(credentials.token_path()).st_mode)
    assert mode == 0o600
    assert list(credentials.token_path().parent.glob("*.tmp")) == []  # no temp file left behind
    credentials.delete_token()
    assert credentials.read_token() is None
    credentials.delete_token()  # deleting twice is fine


def test_overwrite_replaces_token():
    credentials.write_token(TOKEN)
    credentials.write_token(TOKEN[:-1] + "y")
    assert credentials.read_token().endswith("y")


@pytest.mark.parametrize("bad", ["", "short", "has space " + "x" * 30, "tab\t" + "x" * 30, "nönascii" + "x" * 30, "x" * 1001, "line\nbreak" + "x" * 30])
def test_invalid_tokens_rejected_and_nothing_written(bad):
    with pytest.raises(credentials.InvalidToken):
        credentials.write_token(bad)
    assert credentials.read_token() is None


def test_source_prefers_file_over_env(monkeypatch):
    monkeypatch.setattr(get_settings(), "claude_code_oauth_token", "env-token-" + "e" * 20)
    assert credentials.source() == "env"
    credentials.write_token(TOKEN)
    assert credentials.source() == "file"


async def test_claude_code_uses_file_token_over_env_and_rereads(tmp_path):
    (tmp_path / "s").mkdir()
    stub = StubClaude(tmp_path / "s", [{"stdout": envelope(result=x)} for x in "abc"])
    ai = ClaudeCode(claude_bin=str(stub.path), scratch_dir=str(tmp_path), oauth_token="ENV-TOKEN-" + "e" * 20)

    await ai.run("p", system="s")                      # no file: env token
    credentials.write_token(TOKEN)
    await ai.run("p", system="s")                      # file saved after the client was built: used immediately
    credentials.delete_token()
    await ai.run("p", system="s")                      # disconnected: back to env
    seen = [c["env"].get("CLAUDE_CODE_OAUTH_TOKEN") for c in stub.calls]
    assert seen == ["ENV-TOKEN-" + "e" * 20, TOKEN, "ENV-TOKEN-" + "e" * 20]


async def test_no_token_anywhere_passes_none(tmp_path):
    (tmp_path / "s").mkdir()
    stub = StubClaude(tmp_path / "s", [{"stdout": envelope(result="ok")}])
    await ClaudeCode(claude_bin=str(stub.path), scratch_dir=str(tmp_path), oauth_token="").run("p", system="s")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in stub.calls[0]["env"]


async def test_token_never_logged(tmp_path, caplog):
    credentials.write_token(TOKEN)
    (tmp_path / "s").mkdir()
    stub = StubClaude(tmp_path / "s", [{"stdout": envelope(is_error=True, result="Not logged in")}])
    ai = ClaudeCode(claude_bin=str(stub.path), scratch_dir=str(tmp_path))
    with caplog.at_level(logging.DEBUG), pytest.raises(Exception) as e:
        await ai.run("p", system="s")
    assert TOKEN not in caplog.text and TOKEN not in str(e.value)
