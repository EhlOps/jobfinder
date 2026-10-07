"""Where the Claude token lives when it is set from the admin page instead of an env var.

A single file on the `claude-auth` docker volume, shared by the api (which writes it) and the
worker (which reads it before every AI call, so a change applies immediately, no restart).
It is a secret: written 0600, never logged, never returned by any API, and kept out of both
Postgres and the document store."""
import os
from pathlib import Path
from typing import Literal

from jobfinder.config import get_settings

MIN_LEN, MAX_LEN = 20, 1000


class InvalidToken(ValueError):
    pass


def token_path() -> Path:
    return Path(get_settings().claude_auth_dir) / "oauth_token"


def read_token() -> str | None:
    try:
        return token_path().read_text().strip() or None
    except OSError:
        return None


def validate(token: str) -> str:
    token = token.strip()
    if not (MIN_LEN <= len(token) <= MAX_LEN) or not token.isascii() or not token.isprintable() or any(ch.isspace() for ch in token):
        raise InvalidToken("That doesn't look like a token from `claude setup-token`.")
    return token


def write_token(token: str) -> None:
    token = validate(token)
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never world-readable, even briefly
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.replace(tmp, path)


def delete_token() -> None:
    token_path().unlink(missing_ok=True)


def source() -> Literal["file", "env", "none"]:
    if read_token():
        return "file"
    return "env" if get_settings().claude_code_oauth_token else "none"
