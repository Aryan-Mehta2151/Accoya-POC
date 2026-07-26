"""Strict JWT access-token utilities for browser sessions."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt

from app.config import Settings, get_settings


JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_LIFETIME = timedelta(hours=8)


def create_access_token(
    *,
    user_id: str,
    auth_version: int,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> str:
    """Create an eight-hour, purpose-bound browser access token."""

    configured = settings or get_settings()
    if len(configured.jwt_secret_key.encode("utf-8")) < 32:
        raise RuntimeError("JWT_SECRET_KEY must be at least 32 bytes")
    issued_at = now or datetime.now(timezone.utc)
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=timezone.utc)
    issued_at = issued_at.astimezone(timezone.utc).replace(microsecond=0)
    payload = {
        "sub": user_id,
        "token_use": "access",
        "iss": configured.jwt_issuer,
        "aud": configured.jwt_audience,
        "iat": issued_at,
        "exp": issued_at + ACCESS_TOKEN_LIFETIME,
        "jti": secrets.token_urlsafe(24),
        "auth_version": auth_version,
    }
    return jwt.encode(
        payload,
        configured.jwt_secret_key,
        algorithm=JWT_ALGORITHM,
    )


def verify_access_token(
    token: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Decode a valid access token, returning ``None`` for every rejection."""

    configured = settings or get_settings()
    if len(configured.jwt_secret_key.encode("utf-8")) < 32:
        return None
    try:
        payload = jwt.decode(
            token,
            configured.jwt_secret_key,
            algorithms=[JWT_ALGORITHM],
            audience=configured.jwt_audience,
            issuer=configured.jwt_issuer,
            options={
                "require_sub": True,
                "require_exp": True,
                "require_iat": True,
                "require_aud": True,
                "require_iss": True,
            },
        )
    except JWTError:
        return None

    if payload.get("token_use") != "access":
        return None
    if not isinstance(payload.get("jti"), str) or not payload["jti"]:
        return None
    auth_version = payload.get("auth_version")
    if isinstance(auth_version, bool) or not isinstance(auth_version, int):
        return None
    if auth_version < 0:
        return None
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if (
        isinstance(issued_at, bool)
        or not isinstance(issued_at, int)
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or expires_at - issued_at
        != int(ACCESS_TOKEN_LIFETIME.total_seconds())
    ):
        return None
    return payload


# Transitional alias for callers that only need strict access-token decoding.
verify_token = verify_access_token
