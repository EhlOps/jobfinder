"""Settings: names and types only. Values come from `backend/.env.defaults` (committed: every key, its
default and a comment, grouped in this order), then `backend/.env` (gitignored: secrets, overrides), then
the process environment; later wins. Paths are relative to the working directory (`backend/`, `/app`).
No default lives in Python: a missing key or bad value stops startup naming every such key. Env names
are the field names in upper case. Read `settings.x` (loaded on first use) or `get_settings()`."""
from functools import lru_cache
from typing import Annotated, Literal, cast

from pydantic import AfterValidator, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


def _secret_key(value: str) -> str:
    if len(value) < 32:  # also rejects "" and the old "dev-insecure-change-me"
        raise ValueError("must be a random string of at least 32 characters. Generate one with: "
                         'python -c "import secrets; print(secrets.token_urlsafe(48))"')
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env.defaults", ".env"), extra="ignore")
    database_url: str
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str
    postgres_db: str
    secret_key: Annotated[str, AfterValidator(_secret_key)]
    public_base_url: str
    store_dir: str
    upload_max_bytes: int
    log_level: str
    session_ttl_days: int
    claude_bin: str
    claude_scratch_dir: str
    claude_code_oauth_token: str
    claude_auth_dir: str
    ai_max_concurrency: int
    ai_max_source_chars: int
    ai_max_job_chars: int
    ai_check_model: str
    profile_model: str
    followup_model: str
    audit_model: str
    match_model: str
    letter_model: str
    questions_model: str
    resume_model: str
    recipe_model: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_starttls: bool
    email_from: str
    github_token: str
    enable_jobspy: bool
    jobspy_max_queries: int
    jobspy_results_per_query: int
    usajobs_api_key: str
    usajobs_email: str
    usajobs_max_locations: int
    usajobs_max_pages: int
    usajobs_days_posted: int
    scheduler_enabled: bool
    ingest_interval_hours: int
    adaptive_schedule_enabled: bool
    ingest_tick_minutes: int
    ingest_min_interval_minutes: int
    ingest_max_interval_minutes: int
    ingest_base_interval_minutes: int
    ingest_churn_pivot: int
    ingest_jitter: float
    ingest_concurrency: int
    ingest_max_consecutive_failures: int
    ingest_max_discovered_boards_per_run: int
    discovery_enabled: bool
    discovery_max_candidates: int
    job_links_enabled: bool
    job_links_per_user_day: int
    coverage_enabled: bool
    coverage_resolves_per_run: int
    resolver_llm_enabled: bool
    renderer_enabled: bool
    renderer_max_rendered_boards: int
    renderer_concurrency: int
    renderer_poll_seconds: float
    match_daily_llm_budget: int
    match_min_prefilter: float
    digest_min_score: int
    digest_max_matches: int
    digest_send_window_hours: int
    questions_per_email: int
    max_open_questions: int
    planner_interval_minutes: int
    planner_max_per_tick: int
    planner_stagger_seconds: int
    planner_min_interval_hours: int
    planner_backoff_minutes: int
    task_max_attempts: int
    task_stuck_after_minutes: int
    task_backoff_seconds: int
    task_heartbeat_seconds: float
    task_prune_days: int
    worker_poll_seconds: float
    worker_reap_seconds: float
    tasks_backend: Literal["legacy", "ledger"]
    valkey_url: str
    worker_io_concurrency: int
    relay_seconds: float
    password_link_ttl_minutes: int
    link_requests_per_email_hour: int
    link_cooldown_seconds: int
    link_requests_per_ip_hour: int
    login_failures_per_email: int
    login_failures_per_ip: int
    login_failure_window_minutes: int
    preview_emails_per_hour: int

    @property
    def usajobs_enabled(self) -> bool:
        return bool(self.usajobs_api_key and self.usajobs_email)


class SettingsError(SystemExit):
    """Bad configuration: an entrypoint prints the message and exits with status 1."""


def load_settings(**overrides: object) -> Settings:
    """Read the env files and environment now. Raises SettingsError naming every missing/bad key."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as e:
        missing = [str(x["loc"][0]).upper() for x in e.errors() if x["type"] == "missing"]
        bad = [f"{str(x['loc'][0]).upper()}: {x['msg']}" for x in e.errors() if x["type"] != "missing"]
        lines = [f"missing keys: {', '.join(missing)}"] if missing else []
        raise SettingsError("\n  ".join(["Invalid configuration (backend/.env.defaults):", *lines, *bad]))


@lru_cache
def get_settings() -> Settings:
    return load_settings()


class _ProcessSettings:
    """`settings.x` reads and writes `get_settings().x`."""

    def __getattr__(self, name: str) -> object:
        return getattr(get_settings(), name)

    def __setattr__(self, name: str, value: object) -> None:
        setattr(get_settings(), name, value)


settings = cast(Settings, _ProcessSettings())
