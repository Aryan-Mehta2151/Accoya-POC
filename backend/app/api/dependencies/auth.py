"""Central browser authentication and CSRF dependencies."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Request, Security
from fastapi.security import APIKeyCookie
from sqlalchemy import select

from app.config import get_settings
from app.db.database import SessionLocal
from app.db.models import User
from app.services.auth_security import (
    SESSION_COOKIE_NAME,
    authentication_required_exception,
    validate_csrf_request,
)
from app.services.jwt_service import verify_access_token


_session_cookie = APIKeyCookie(name=SESSION_COOKIE_NAME, auto_error=False)


def get_current_user(
    request: Request,
    session_token: Annotated[str | None, Security(_session_cookie)],
) -> User:
    """Authenticate with a short-lived session detached from route work."""

    settings = get_settings()
    if not session_token:
        raise authentication_required_exception(settings)

    payload = verify_access_token(session_token, settings=settings)
    if payload is None:
        raise authentication_required_exception(settings)

    try:
        user_id = str(uuid.UUID(str(payload["sub"])))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise authentication_required_exception(settings)

    # Authentication must not share the route's request-scoped session. A
    # SELECT autobegins a transaction, and sharing that session would keep the
    # transaction open while routes call EarlyBid, S3, Bedrock, or Gemini.
    with SessionLocal() as auth_db:
        user = auth_db.execute(
            select(User).where(User.id == user_id)
        ).scalars().first()
        if (
            user is None
            or not user.is_active
            or user.auth_version != payload.get("auth_version")
        ):
            raise authentication_required_exception(settings)

        # Preserve only already-loaded scalar identity fields after closing the
        # auth transaction; route code must use its own session for mutations.
        auth_db.expunge(user)
        auth_db.rollback()

    user.session_expires_at = datetime.fromtimestamp(
        payload["exp"],
        tz=timezone.utc,
    )

    request.state.current_user = user
    return user


def require_csrf(request: Request) -> None:
    """Reject an unsafe request without the matching CSRF header token."""

    validate_csrf_request(request)
