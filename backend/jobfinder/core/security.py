"""Password hashing and random tokens. Pure functions: no database, no settings.

Passwords use argon2 (slow on purpose). Session tokens are 256 random bits, so storing their plain
SHA-256 is enough (no salt or slow hash): a leaked sessions table can't be used to sign in.
"""
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """False for a wrong password and for a malformed hash; never raises for either."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def new_token() -> str:
    """A URL-safe token with 256 bits of randomness (session cookies)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """The digest stored for a token: compare digests, never raw tokens."""
    return hashlib.sha256(token.encode()).hexdigest()
