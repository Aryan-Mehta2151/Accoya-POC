"""Public login/recovery routes and protected session routes."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies.auth import get_current_user, require_csrf
from app.config import Settings, get_settings
from app.db.database import get_db
from app.db.models import PasswordResetToken, User
from app.schemas.auth import (
    CsrfResponse,
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    MessageResponse,
    ResetPasswordRequest,
    UserResponse,
)
from app.services.auth_security import (
    GOOGLE_NONCE_COOKIE_NAME,
    GOOGLE_PKCE_COOKIE_NAME,
    GOOGLE_STATE_COOKIE_NAME,
    clear_csrf_cookie,
    clear_google_oauth_cookies,
    clear_session_cookie,
    csrf_token_for_request,
    hash_password_reset_token,
    issue_csrf_token,
    normalize_email,
    set_google_oauth_cookies,
    set_session_cookie,
)
from app.services.email_service import send_password_reset_email
from app.services.jwt_service import ACCESS_TOKEN_LIFETIME, create_access_token
from app.services.oauth_service import (
    GoogleOAuthError,
    exchange_google_code,
    google_authorization_url,
    verify_google_id_token,
)
from app.services.password_service import hash_password, verify_password


router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

_FORGOT_MESSAGE = (
    "If the account is eligible, a password reset link will be sent."
)
_DUMMY_PASSWORD_HASH = (
    "$2b$12$pMPJpk0aw7UV8F9ujzRXCueziv2/11.GudzBfKNiXLHapzXvt8qMi"
)


def _user_response(user: User) -> UserResponse:
    return UserResponse.model_validate(user)


def _invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "invalid_credentials",
            "message": "Email or password is incorrect.",
        },
    )


def _invalid_reset_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "invalid_reset_token",
            "message": "The password reset link is invalid or expired.",
        },
    )


def _set_authenticated_session(
    response: Response,
    user: User,
    settings: Settings,
) -> str:
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    token = create_access_token(
        user_id=user.id,
        auth_version=user.auth_version,
        settings=settings,
        now=issued_at,
    )
    user.session_expires_at = issued_at + ACCESS_TOKEN_LIFETIME
    set_session_cookie(response, token, settings)
    return issue_csrf_token(response, settings)


def _google_frontend_response(
    settings: Settings,
    *,
    error_code: str | None = None,
) -> RedirectResponse:
    suffix = f"?error={error_code}" if error_code else ""
    response = RedirectResponse(
        url=f"{settings.frontend_url.rstrip('/')}/auth/callback{suffix}",
        status_code=status.HTTP_302_FOUND,
    )
    clear_google_oauth_cookies(response, settings)
    return response


@router.get("/csrf", response_model=CsrfResponse)
def get_csrf(
    request: Request,
    response: Response,
) -> CsrfResponse:
    """Create or re-derive CSRF material without exposing its cookie seed."""

    return CsrfResponse(csrf_token=csrf_token_for_request(request, response))


@router.post(
    "/login",
    response_model=LoginResponse,
    dependencies=[Depends(require_csrf)],
)
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> LoginResponse:
    """Authenticate an approved password account and set its session cookie."""

    email = normalize_email(str(payload.email))
    user = db.execute(select(User).where(User.email == email)).scalars().first()
    candidate_hash = (
        user.password_hash
        if user is not None and user.password_hash
        else _DUMMY_PASSWORD_HASH
    )
    password_matches = verify_password(payload.password, candidate_hash)
    if (
        user is None
        or not user.is_active
        or not user.password_hash
        or not password_matches
    ):
        raise _invalid_credentials()

    settings = get_settings()
    csrf_token = _set_authenticated_session(response, user, settings)
    return LoginResponse(user=_user_response(user), csrf_token=csrf_token)


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    dependencies=[Depends(require_csrf)],
)
def forgot_password(
    payload: ForgotPasswordRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Create one hashed reset secret without revealing account existence."""

    settings = get_settings()
    email = normalize_email(str(payload.email))
    user = db.execute(
        select(User).where(User.email == email).with_for_update()
    ).scalars().first()
    if user is None or not user.is_active or not user.password_hash:
        # Keep the password check endpoint timing less useful for enumeration.
        verify_password("not-a-real-password", _DUMMY_PASSWORD_HASH)
        return MessageResponse(message=_FORGOT_MESSAGE)

    recipient_email = user.email
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.password_reset_token_expire_minutes
    )
    db.execute(
        delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
    )
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_password_reset_token(raw_token),
            expires_at=expires_at,
        )
    )
    db.commit()

    reset_link = (
        f"{settings.frontend_url.rstrip('/')}/reset-password#token={raw_token}"
    )
    if not send_password_reset_email(recipient_email, reset_link):
        logger.error("Password reset email delivery failed")
    return MessageResponse(message=_FORGOT_MESSAGE)


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    dependencies=[Depends(require_csrf)],
)
def reset_password(
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Consume one reset secret and revoke every existing user session."""

    token_hash = hash_password_reset_token(payload.token)
    candidate = db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash
        )
    ).scalars().first()
    if candidate is None:
        raise _invalid_reset_token()

    user = db.execute(
        select(User).where(User.id == candidate.user_id).with_for_update()
    ).scalars().first()
    if user is None:
        raise _invalid_reset_token()

    # Re-read under the user lock so reset and administrative password changes
    # use one consistent lock order and a reset secret remains single-use.
    reset_record = db.execute(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == token_hash)
        .with_for_update()
    ).scalars().first()
    if reset_record is None:
        raise _invalid_reset_token()

    expires_at = reset_record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc) or not user.is_active:
        db.delete(reset_record)
        db.commit()
        raise _invalid_reset_token()

    user.password_hash = hash_password(payload.password)
    user.auth_version += 1
    db.execute(
        delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
    )
    db.commit()
    return MessageResponse(message="Password reset successfully.")


@router.get("/google/start")
def google_start() -> RedirectResponse:
    """Start the server-owned Google OIDC flow."""

    settings = get_settings()
    if not settings.google_client_id.strip() or not settings.google_client_secret.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "google_login_unavailable",
                "message": "Google sign-in is not configured.",
            },
        )

    state_value = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    response = RedirectResponse(
        url=google_authorization_url(
            state=state_value,
            nonce=nonce,
            verifier=verifier,
            settings=settings,
        ),
        status_code=status.HTTP_302_FOUND,
    )
    set_google_oauth_cookies(
        response,
        state=state_value,
        nonce=nonce,
        verifier=verifier,
        settings=settings,
    )
    return response


@router.get("/callback/google")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Finish Google login for a pre-approved, active local account."""

    settings = get_settings()
    saved_state = request.cookies.get(GOOGLE_STATE_COOKIE_NAME)
    nonce = request.cookies.get(GOOGLE_NONCE_COOKIE_NAME)
    verifier = request.cookies.get(GOOGLE_PKCE_COOKIE_NAME)
    if (
        error is not None
        or not code
        or not state
        or not saved_state
        or not nonce
        or not verifier
        or not secrets.compare_digest(state, saved_state)
    ):
        error_code = "access_denied" if error == "access_denied" else "oauth_failed"
        return _google_frontend_response(settings, error_code=error_code)

    try:
        raw_id_token = await exchange_google_code(
            code=code,
            verifier=verifier,
            settings=settings,
        )
        identity = await verify_google_id_token(
            raw_id_token=raw_id_token,
            expected_nonce=nonce,
            settings=settings,
        )
    except GoogleOAuthError:
        return _google_frontend_response(settings, error_code="oauth_failed")

    user = db.execute(
        select(User).where(User.email == identity.email).with_for_update()
    ).scalars().first()
    if user is None or not user.is_active:
        return _google_frontend_response(settings, error_code="oauth_failed")

    identity_owner = db.execute(
        select(User).where(
            User.oauth_provider == "google",
            User.oauth_id == identity.subject,
        )
    ).scalars().first()
    if identity_owner is not None and identity_owner.id != user.id:
        return _google_frontend_response(settings, error_code="oauth_failed")

    if user.oauth_provider is None and user.oauth_id is None:
        user.oauth_provider = "google"
        user.oauth_id = identity.subject
        if not user.name and identity.name:
            user.name = identity.name
    elif user.oauth_provider != "google" or user.oauth_id != identity.subject:
        return _google_frontend_response(settings, error_code="oauth_failed")

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _google_frontend_response(settings, error_code="oauth_failed")

    response = _google_frontend_response(settings)
    _set_authenticated_session(response, user, settings)
    return response


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)) -> UserResponse:
    """Return the database-authoritative current account."""

    return _user_response(current_user)


@router.post(
    "/logout",
    response_model=MessageResponse,
)
def logout(
    response: Response,
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Revoke every current access token for the account and clear cookies."""

    locked_user = db.execute(
        select(User).where(User.id == current_user.id).with_for_update()
    ).scalar_one()
    locked_user.auth_version += 1
    db.commit()

    settings = get_settings()
    clear_session_cookie(response, settings)
    clear_csrf_cookie(response, settings)
    return MessageResponse(message="Signed out.")
