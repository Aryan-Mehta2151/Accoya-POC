"""Public login/recovery routes and protected session routes."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies.auth import get_current_user, require_csrf
from app.config import Settings, get_settings
from app.db.database import get_db
from app.db.models import AccessRequest, AccessRequestStatus, PasswordResetToken, User
from app.schemas.auth import (
    AccessRequestCreate,
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
from app.services.email_service import (
    send_access_request_decision_email,
    send_access_request_review_email,
    send_password_reset_email,
)
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
_ACCESS_REQUEST_MESSAGE = (
    "If access can be granted, the request will be reviewed shortly."
)
_ALREADY_HAS_ACCESS_MESSAGE = (
    "This email already has access. Please sign in or use Forgot Password."
)
_ACCESS_REQUEST_COOLDOWN_MESSAGE = (
    "A request was already submitted recently. Please wait before trying again."
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


def _invalid_access_request_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "invalid_access_request_token",
            "message": "The access-review link is invalid or expired.",
        },
    )


def _api_origin_from_settings(settings: Settings) -> str:
    parsed = urlparse(settings.google_redirect_uri)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "http://localhost:8000"


def _review_link(
    settings: Settings,
    *,
    token: str,
    decision: str,
) -> str:
    api_origin = _api_origin_from_settings(settings)
    prefix = settings.api_prefix.rstrip("/")
    query = urlencode({"token": token, "decision": decision})
    return f"{api_origin}{prefix}/auth/access-requests/review?{query}"


def _review_result_page(*, approved: bool, requester_email: str) -> HTMLResponse:
        if approved:
                title = "Access Request Approved"
                body = (
                        "The requester has been approved. A password setup email has been "
                        f"sent to {requester_email}."
                )
                accent = "#065f46"
                background = "#ecfdf5"
        else:
                title = "Access Request Rejected"
                body = (
                        f"The request from {requester_email} was rejected. "
                        "No account changes were applied."
                )
                accent = "#991b1b"
                background = "#fef2f2"

        html = f"""
        <!doctype html>
        <html lang=\"en\">
            <head>
                <meta charset=\"utf-8\" />
                <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
                <title>{title}</title>
            </head>
            <body style=\"margin:0; padding:0; background:#f8f7f4; font-family:Segoe UI, Arial, sans-serif; color:#1f2937;\">
                <main style=\"min-height:100vh; display:grid; place-items:center; padding:24px;\">
                    <section style=\"width:100%; max-width:560px; background:{background}; border:1px solid #d1d5db; border-radius:14px; padding:28px; box-shadow:0 8px 22px rgba(0,0,0,0.08);\">
                        <p style=\"margin:0 0 8px; font-size:13px; letter-spacing:0.06em; text-transform:uppercase; color:{accent};\">Accoya Access Review</p>
                        <h1 style=\"margin:0 0 10px; font-size:28px; line-height:1.2; color:{accent};\">{title}</h1>
                        <p style=\"margin:0; font-size:15px; line-height:1.6;\">{body}</p>
                    </section>
                </main>
            </body>
        </html>
        """
        return HTMLResponse(content=html, status_code=status.HTTP_200_OK)


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
    "/request-access",
    response_model=MessageResponse,
    dependencies=[Depends(require_csrf)],
)
def request_access(
    payload: AccessRequestCreate,
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Capture a pending enrollment request and notify the configured approver."""

    settings = get_settings()
    email = normalize_email(str(payload.email))
    request_name = payload.name.strip() if payload.name else None

    existing_user = db.execute(
        select(User).where(User.email == email)
    ).scalars().first()
    if existing_user is not None and existing_user.is_active:
        return MessageResponse(message=_ALREADY_HAS_ACCESS_MESSAGE)

    now = datetime.now(timezone.utc)
    cooldown_window = timedelta(
        minutes=settings.access_request_cooldown_minutes
    )

    request_record = db.execute(
        select(AccessRequest)
        .where(
            AccessRequest.email == email,
            AccessRequest.status == AccessRequestStatus.pending,
        )
        .with_for_update()
    ).scalars().first()

    if request_record is None:
        expires_at = now + timedelta(
            minutes=settings.access_request_token_expire_minutes
        )
        raw_token = secrets.token_urlsafe(32)
        token_hash = hash_password_reset_token(raw_token)
        request_record = AccessRequest(
            email=email,
            name=request_name,
            status=AccessRequestStatus.pending,
            token_hash=token_hash,
            expires_at=expires_at,
            requested_at=now,
        )
        db.add(request_record)
    else:
        requested_at = request_record.requested_at
        if requested_at.tzinfo is None:
            requested_at = requested_at.replace(tzinfo=timezone.utc)
        if requested_at + cooldown_window > now:
            return MessageResponse(message=_ACCESS_REQUEST_COOLDOWN_MESSAGE)

        expires_at = now + timedelta(
            minutes=settings.access_request_token_expire_minutes
        )
        raw_token = secrets.token_urlsafe(32)
        token_hash = hash_password_reset_token(raw_token)
        request_record.name = request_name
        request_record.token_hash = token_hash
        request_record.expires_at = expires_at
        request_record.requested_at = now

    db.commit()

    approved_link = _review_link(
        settings,
        token=raw_token,
        decision="approve",
    )
    reject_link = _review_link(
        settings,
        token=raw_token,
        decision="reject",
    )
    if not send_access_request_review_email(
        approver_email=str(settings.access_request_approver_email),
        requester_email=email,
        requester_name=request_name,
        approve_link=approved_link,
        reject_link=reject_link,
    ):
        logger.error("Access request notification email delivery failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "access_request_notification_failed",
                "message": "Access request notification could not be delivered.",
            },
        )

    return MessageResponse(message=_ACCESS_REQUEST_MESSAGE)


@router.get(
    "/access-requests/review",
)
def review_access_request(
    token: str = Query(min_length=32, max_length=512),
    decision: str = Query(pattern="^(approve|reject)$"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Approve or reject one pending access request via a signed email link."""

    settings = get_settings()
    token_hash = hash_password_reset_token(token)
    request_record = db.execute(
        select(AccessRequest)
        .where(
            AccessRequest.token_hash == token_hash,
            AccessRequest.status == AccessRequestStatus.pending,
        )
        .with_for_update()
    ).scalars().first()
    if request_record is None:
        raise _invalid_access_request_token()

    expires_at = request_record.expires_at
    if expires_at is None:
        raise _invalid_access_request_token()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    if expires_at <= now:
        request_record.status = AccessRequestStatus.expired
        request_record.token_hash = None
        request_record.expires_at = None
        request_record.reviewed_at = now
        request_record.reviewed_by = str(settings.access_request_approver_email)
        db.commit()
        raise _invalid_access_request_token()

    if decision == "approve":
        user = db.execute(
            select(User).where(User.email == request_record.email).with_for_update()
        ).scalars().first()
        if user is None:
            user = User(
                email=request_record.email,
                name=request_record.name,
                is_active=True,
                auth_version=0,
            )
            db.add(user)
            db.flush()
        else:
            user.is_active = True
            if not user.name and request_record.name:
                user.name = request_record.name

        raw_reset_token = secrets.token_urlsafe(32)
        reset_expires_at = now + timedelta(
            minutes=settings.password_reset_token_expire_minutes
        )
        db.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
        )
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_password_reset_token(raw_reset_token),
                expires_at=reset_expires_at,
            )
        )

        request_record.status = AccessRequestStatus.approved
        request_record.token_hash = None
        request_record.expires_at = None
        request_record.reviewed_at = now
        request_record.reviewed_by = str(settings.access_request_approver_email)
        request_record.reviewed_user_id = user.id
        db.commit()

        reset_link = (
            f"{settings.frontend_url.rstrip('/')}/reset-password#token={raw_reset_token}"
        )
        if not send_access_request_decision_email(
            recipient_email=request_record.email,
            approved=True,
            reset_link=reset_link,
        ):
            logger.error("Access approval email delivery failed")
        return _review_result_page(
            approved=True,
            requester_email=request_record.email,
        )

    request_record.status = AccessRequestStatus.rejected
    request_record.token_hash = None
    request_record.expires_at = None
    request_record.reviewed_at = now
    request_record.reviewed_by = str(settings.access_request_approver_email)
    db.commit()

    if not send_access_request_decision_email(
        recipient_email=request_record.email,
        approved=False,
    ):
        logger.error("Access rejection email delivery failed")
    return _review_result_page(
        approved=False,
        requester_email=request_record.email,
    )


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
        return _google_frontend_response(settings, error_code="access_not_approved")

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
