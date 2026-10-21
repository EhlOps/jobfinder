"""Configuration comes only from env files (#39): backend/.env.defaults lists every key, backend/.env
overrides it, the process environment overrides both, and a missing or bad key stops startup."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from jobfinder.core.config import Settings, SettingsError, get_settings, load_settings, settings

BACKEND = Path(__file__).resolve().parents[1]
DEFAULTS = BACKEND / ".env.defaults"
SECRET = "s" * 40


def _keys() -> list[str]:
    return list(dotenv_values(DEFAULTS))


def test_defaults_file_and_settings_list_the_same_keys_in_the_same_order():
    fields = [name.upper() for name in Settings.model_fields]
    assert len(set(_keys())) == len(_keys()), "a key is listed twice in .env.defaults"
    assert set(fields) - set(_keys()) == set(), "Settings fields missing from .env.defaults"
    assert set(_keys()) - set(fields) == set(), ".env.defaults keys that Settings doesn't declare"
    assert fields == _keys(), "keep .env.defaults in the same order as core/config.py"


def test_no_setting_has_a_python_default():
    assert [n for n, f in Settings.model_fields.items() if not f.is_required()] == []


def test_defaults_file_holds_no_secret():
    values = dotenv_values(DEFAULTS)
    for key in ("SECRET_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "SMTP_PASSWORD", "GITHUB_TOKEN", "USAJOBS_API_KEY"):
        assert values[key] == "", key


def test_email_links_default_to_the_dev_web_server():
    """The Vite dev server, as `jobfinder dev` sets it (tests/test_cli_dev.py), not the old compose web port."""
    assert dotenv_values(DEFAULTS)["PUBLIC_BASE_URL"] == "http://localhost:5173"


def _isolated(tmp_path, monkeypatch, defaults: str, dotenv: str | None = None) -> None:
    """Run in a directory holding only these env files, with no setting in the environment."""
    (tmp_path / ".env.defaults").write_text(defaults)
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv)
    for key in _keys():
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def test_defaults_plus_secrets_in_dotenv_is_enough(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch, DEFAULTS.read_text(), f"SECRET_KEY={SECRET}\n")
    s = load_settings()
    assert s.secret_key == SECRET and s.match_model == "sonnet" and s.ingest_jitter == 0.2
    assert s.email_from == "JobFinder <jobs@localhost>" and s.smtp_starttls is False


def test_dotenv_overrides_defaults_and_the_environment_overrides_both(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch, DEFAULTS.read_text(), f"SECRET_KEY={SECRET}\nMATCH_MODEL=opus\nDIGEST_MIN_SCORE=70\n")
    monkeypatch.setenv("DIGEST_MIN_SCORE", "80")
    s = load_settings()
    assert (s.match_model, s.digest_min_score, s.recipe_model) == ("opus", 80, "sonnet")


def test_missing_keys_are_all_named(tmp_path, monkeypatch):
    kept = [line for line in DEFAULTS.read_text().splitlines()
            if not line.startswith(("SMTP_HOST=", "MATCH_MODEL="))]
    _isolated(tmp_path, monkeypatch, "\n".join(kept), f"SECRET_KEY={SECRET}\n")
    with pytest.raises(SettingsError) as e:
        load_settings()
    assert "missing keys: MATCH_MODEL, SMTP_HOST" in str(e.value)


def test_bad_values_name_their_key(tmp_path, monkeypatch):
    _isolated(tmp_path, monkeypatch, DEFAULTS.read_text(), "SECRET_KEY=short\nSMTP_PORT=many\n")
    with pytest.raises(SettingsError) as e:
        load_settings()
    assert "SMTP_PORT:" in str(e.value) and "SECRET_KEY: Value error, must be a random string" in str(e.value)


def test_a_missing_key_exits_non_zero_and_prints_it(tmp_path):
    kept = [line for line in DEFAULTS.read_text().splitlines() if not line.startswith("PLANNER_MAX_PER_TICK=")]
    (tmp_path / ".env.defaults").write_text("\n".join(kept))
    env = {k: v for k, v in os.environ.items() if k not in _keys()} | {"SECRET_KEY": SECRET}
    run = subprocess.run(
        [sys.executable, "-c", "from jobfinder.core.config import get_settings; get_settings()"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert run.returncode == 1
    assert "missing keys: PLANNER_MAX_PER_TICK" in run.stderr and "Traceback" not in run.stderr


def test_process_settings_view_reads_and_writes_the_cached_settings(monkeypatch):
    assert settings.digest_max_matches == get_settings().digest_max_matches
    monkeypatch.setattr(settings, "digest_max_matches", 99)
    assert get_settings().digest_max_matches == 99


def test_defaults_file_is_plain_key_value_lines():
    """Values stay plain KEY=value lines (no inline comments), which every dotenv parser agrees on."""
    for line in DEFAULTS.read_text().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            assert key.isupper() and " #" not in value, line
