from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://jobfinder:change-me@localhost:5432/jobfinder"
    secret_key: str = ""  # required: signs email links; see _check_secret_key
    public_base_url: str = "http://localhost:8180"
    store_dir: str = "./store"  # document file store (docker volume in compose)

    # AI runs through the Claude Code CLI on a subscription (no API key). In docker, set the
    # token from `claude setup-token`; on a dev machine the CLI's own login is used.
    claude_bin: str = "claude"
    claude_scratch_dir: str = "/tmp"
    claude_code_oauth_token: str = ""  # fallback; the admin page stores a token under claude_auth_dir
    claude_auth_dir: str = "./claude-auth"  # credentials volume (compose: /data/claude), shared by api + worker
    admin_emails: str = ""  # comma-separated; the only way to be admin
    ai_max_concurrency: int = 2

    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = False
    email_from: str = "JobFinder <jobs@localhost>"

    github_token: str = ""
    enable_jobspy: bool = False
    scheduler_enabled: bool = True  # worker runs ingestion and the daily emails on a schedule
    ingest_interval_hours: int = 6
    digest_min_score: int = 65  # only matches at least this good are included in the daily email
    digest_max_matches: int = 10
    questions_per_email: int = 6
    max_open_questions: int = 15  # stop collecting new questions while this many are unanswered
    ingest_concurrency: int = 5
    match_daily_llm_budget: int = 25  # LLM-scored jobs per user per rolling 24h
    match_model: str = "sonnet"  # model that scores jobs: haiku | sonnet | opus
    match_min_prefilter: float = 20.0  # candidates scoring below this (0-100) never reach the LLM
    jobspy_max_queries: int = 10
    jobspy_results_per_query: int = 25
    session_ttl_days: int = 30

    password_link_ttl_minutes: int = 60
    link_requests_per_email_hour: int = 3
    link_cooldown_seconds: int = 60
    link_requests_per_ip_hour: int = 10
    login_failures_per_email: int = 10
    login_failures_per_ip: int = 30
    login_failure_window_minutes: int = 15
    preview_emails_per_hour: int = 5

    @model_validator(mode="after")
    def _check_secret_key(self) -> "Settings":
        if self.secret_key in ("", "dev-insecure-change-me") or len(self.secret_key) < 32:
            raise ValueError(
                "SECRET_KEY must be set to a random string of at least 32 characters. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
