"""Signed, expiring tokens for links in emails (answer questions, unsubscribe) so they work without a login.
Each purpose has its own salt, so a token for one action can't be replayed for another."""
import hashlib

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from jobfinder.config import get_settings

MAX_AGE = {"questions": 14 * 86400, "unsubscribe": 365 * 86400}


class InvalidToken(Exception):
    def __init__(self, expired: bool = False):
        super().__init__("expired" if expired else "invalid")
        self.expired = expired


def _serializer(purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=f"jobfinder-{purpose}")


def make_token(purpose: str, user_id: int) -> str:
    return _serializer(purpose).dumps({"u": user_id})


def read_token(purpose: str, token: str) -> int:
    try:
        data = _serializer(purpose).loads(token, max_age=MAX_AGE[purpose])
        return int(data["u"])
    except SignatureExpired:
        raise InvalidToken(expired=True) from None
    except (BadSignature, KeyError, TypeError, ValueError):
        raise InvalidToken from None


def password_fingerprint(password_hash: str | None) -> str:
    """Changes whenever the password does (empty for pending accounts), which makes a reset link single use."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16] if password_hash else ""


def make_password_token(user_id: int, password_hash: str | None) -> str:
    return _serializer("password-reset").dumps({"u": user_id, "f": password_fingerprint(password_hash)})


def read_password_token(token: str) -> tuple[int, str]:
    try:
        data = _serializer("password-reset").loads(token, max_age=get_settings().password_link_ttl_minutes * 60)
        return int(data["u"]), str(data["f"])
    except SignatureExpired:
        raise InvalidToken(expired=True) from None
    except (BadSignature, KeyError, TypeError, ValueError):
        raise InvalidToken from None
