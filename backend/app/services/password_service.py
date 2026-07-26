"""Password hashing and validation utilities."""

from __future__ import annotations

import bcrypt


MIN_PASSWORD_CHARACTERS = 12
MAX_PASSWORD_BYTES = 72


def validate_password(password: str) -> str:
    """Return a valid password or raise a user-safe validation error."""

    if len(password) < MIN_PASSWORD_CHARACTERS:
        raise ValueError(
            f"Password must contain at least {MIN_PASSWORD_CHARACTERS} characters."
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password must be at most {MAX_PASSWORD_BYTES} UTF-8 bytes."
        )
    return password


def hash_password(password: str) -> str:
    """Validate and hash a password with bcrypt."""

    validated = validate_password(password)
    return bcrypt.hashpw(validated.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password without allowing malformed hashes to escape."""

    try:
        if len(plain_password.encode("utf-8")) > MAX_PASSWORD_BYTES:
            return False
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("ascii"),
        )
    except (TypeError, ValueError, UnicodeError):
        return False
