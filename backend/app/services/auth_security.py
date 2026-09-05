"""Browser-session cookies, CSRF protection, and security configuration."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from base64 import urlsafe_b64encode
from urllib.parse import urlparse

from fastapi import HTTPException, Request, Response, status

from app.config import Settings, get_settings


SESSION_COOKIE_NAME = "accoya_session"
CSRF_COOKIE_NAME = "accoya_csrf_seed"
CSRF_HEADER_NAME = "X-CSRF-Token"
GOOGLE_STATE_COOKIE_NAME = "accoya_google_state"
GOOGLE_NONCE_COOKIE_NAME = "accoya_google_nonce"
GOOGLE_PKCE_COOKIE_NAME = "accoya_google_pkce"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
OAUTH_MAX_AGE_SECONDS = 10 * 60
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_CSRF_MAC_DOMAIN = b"accoya-csrf-v1\x00"
_CSRF_SEED_LENGTH = 43
_CSRF_SEED_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def normalize_email(value: str) -> str:
    """Return the canonical account email representation."""

    return value.strip().lower()


def hash_password_reset_token(token: str) -> str:
    """Hash a one-time reset secret before database lookup or storage."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_web_auth_settings(settings: Settings) -> None:
    """Reject deployment settings that would weaken browser authentication."""

    errors: list[str] = []
    environment = settings.app_env.strip().casefold()
    hardened_environment = environment != "development"

    if len(settings.jwt_secret_key.encode("utf-8")) < 32:
        errors.append("JWT_SECRET_KEY must be at least 32 bytes")
    if len(settings.csrf_secret_key.encode("utf-8")) < 32:
        errors.append("CSRF_SECRET_KEY must be at least 32 bytes")
    if secrets.compare_digest(
        settings.jwt_secret_key.encode("utf-8"),
        settings.csrf_secret_key.encode("utf-8"),
    ):
        errors.append("JWT_SECRET_KEY and CSRF_SECRET_KEY must be different")
    if not settings.jwt_issuer.strip():
        errors.append("JWT_ISSUER must not be blank")
    if not settings.jwt_audience.strip():
        errors.append("JWT_AUDIENCE must not be blank")
    if not 1 <= settings.password_reset_token_expire_minutes <= 60:
        errors.append(
            "PASSWORD_RESET_TOKEN_EXPIRE_MINUTES must be between 1 and 60"
        )
    if not 5 <= settings.access_request_token_expire_minutes <= 10080:
        errors.append(
            "ACCESS_REQUEST_TOKEN_EXPIRE_MINUTES must be between 5 and 10080"
        )
    if not 1 <= settings.access_request_cooldown_minutes <= 10080:
        errors.append(
            "ACCESS_REQUEST_COOLDOWN_MINUTES must be between 1 and 10080"
        )
    if not str(settings.access_request_approver_email).strip():
        errors.append("ACCESS_REQUEST_APPROVER_EMAIL must not be blank")
    if not settings.api_prefix.startswith("/"):
        errors.append("API_PREFIX must start with '/'")

    origins = settings.cors_allowed_origins
    if not origins:
        errors.append("CORS_ALLOWED_ORIGINS must contain at least one origin")
    for origin in origins:
        parsed = urlparse(origin)
        if (
            origin == "*"
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            errors.append(f"Invalid CORS origin: {origin!r}")

    frontend = urlparse(settings.frontend_url)
    frontend_origin: str | None = None
    if (
        frontend.scheme not in {"http", "https"}
        or not frontend.netloc
        or frontend.hostname is None
        or frontend.username is not None
        or frontend.password is not None
        or frontend.path not in {"", "/"}
        or frontend.params
        or frontend.query
        or frontend.fragment
    ):
        errors.append("FRONTEND_URL must be an HTTP(S) origin without a path")
    else:
        frontend_origin = f"{frontend.scheme}://{frontend.netloc}"
        if frontend_origin not in origins:
            errors.append(
                "CORS_ALLOWED_ORIGINS must contain the canonical FRONTEND_URL origin"
            )

    google_callback = urlparse(settings.google_redirect_uri)
    prefix = settings.api_prefix.rstrip("/")
    expected_google_path = f"{prefix}/auth/callback/google"
    if (
        google_callback.scheme not in {"http", "https"}
        or not google_callback.netloc
        or google_callback.hostname is None
        or google_callback.username is not None
        or google_callback.password is not None
        or google_callback.path != expected_google_path
        or google_callback.params
        or google_callback.query
        or google_callback.fragment
    ):
        errors.append(
            "GOOGLE_REDIRECT_URI must exactly target "
            f"{expected_google_path!r} on an HTTP(S) origin"
        )

    google_client_configured = bool(settings.google_client_id.strip())
    google_secret_configured = bool(settings.google_client_secret.strip())
    if google_client_configured != google_secret_configured:
        errors.append(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be configured together"
        )

    if settings.email_reply_tracking_enabled:
        if not all(
            (
                settings.microsoft_client_id.strip(),
                settings.microsoft_tenant_id.strip(),
                settings.microsoft_client_secret.strip(),
                str(settings.microsoft_sender_email).strip(),
            )
        ):
            errors.append(
                "Microsoft Graph credentials and sender must be configured "
                "when reply tracking is enabled"
            )
        notification_url = urlparse(settings.microsoft_graph_notification_url)
        expected_notification_path = (
            f"{prefix}/microsoft-graph/mail-notifications"
        )
        if (
            notification_url.scheme != "https"
            or not notification_url.netloc
            or notification_url.hostname is None
            or notification_url.username is not None
            or notification_url.password is not None
            or notification_url.path != expected_notification_path
            or notification_url.params
            or notification_url.query
            or notification_url.fragment
        ):
            errors.append(
                "MICROSOFT_GRAPH_NOTIFICATION_URL must be a public HTTPS URL "
                f"ending in {expected_notification_path!r}"
            )
        if len(settings.microsoft_graph_client_state.encode("utf-8")) < 32:
            errors.append(
                "MICROSOFT_GRAPH_CLIENT_STATE must be at least 32 bytes"
            )
        reply_timings = (
            settings.email_reply_worker_poll_seconds,
            settings.email_reply_reconcile_seconds,
            settings.email_reply_heartbeat_seconds,
            settings.email_reply_stale_seconds,
        )
        if min(reply_timings) <= 0:
            errors.append("Email reply worker timing settings must be positive")
        if (
            settings.email_reply_stale_seconds
            <= settings.email_reply_heartbeat_seconds
        ):
            errors.append(
                "EMAIL_REPLY_STALE_SECONDS must exceed "
                "EMAIL_REPLY_HEARTBEAT_SECONDS"
            )
        if settings.email_reply_backfill_days <= 0:
            errors.append("EMAIL_REPLY_BACKFILL_DAYS must be positive")

    if hardened_environment:
        if not settings.auth_cookie_secure:
            errors.append("AUTH_COOKIE_SECURE must be true outside development")
        if frontend.scheme != "https" or not frontend.netloc:
            errors.append("FRONTEND_URL must use HTTPS outside development")
        if google_callback.scheme != "https" or not google_callback.netloc:
            errors.append("GOOGLE_REDIRECT_URI must use HTTPS outside development")
        if any(urlparse(origin).scheme != "https" for origin in origins):
            errors.append("All non-development CORS origins must use HTTPS")
        if not google_client_configured or not google_secret_configured:
            errors.append(
                "Google credentials are required outside development"
            )
        if not (
            settings.microsoft_client_id.strip()
            and settings.microsoft_tenant_id.strip()
            and settings.microsoft_client_secret.strip()
            and settings.microsoft_sender_email.strip()
            and settings.microsoft_graph_timeout_seconds > 0
        ):
            errors.append(
                "Microsoft Graph mail delivery must be configured outside development"
            )
        if (
            frontend.hostname
            and google_callback.hostname
            and frontend.hostname != google_callback.hostname
        ):
            errors.append(
                "FRONTEND_URL and GOOGLE_REDIRECT_URI must use the same hostname "
                "so SameSite=Lax authentication cookies are sent"
            )
        if frontend.hostname and any(
            urlparse(origin).hostname != frontend.hostname for origin in origins
        ):
            errors.append(
                "All non-development CORS origins must use the FRONTEND_URL "
                "hostname"
            )

    if errors:
        raise RuntimeError("Invalid web authentication settings: " + "; ".join(errors))


def _cookie_path(settings: Settings) -> str:
    return settings.api_prefix.rstrip("/") or "/"


def _oauth_cookie_path(settings: Settings) -> str:
    prefix = _cookie_path(settings).rstrip("/")
    return f"{prefix}/auth"


def set_session_cookie(
    response: Response,
    token: str,
    settings: Settings | None = None,
) -> None:
    """Set the host-only browser access-token cookie."""

    configured = settings or get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        expires=SESSION_MAX_AGE_SECONDS,
        path=_cookie_path(configured),
        secure=configured.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(
    response: Response,
    settings: Settings | None = None,
) -> None:
    """Expire the browser access-token cookie at its exact path."""

    configured = settings or get_settings()
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=_cookie_path(configured),
        secure=configured.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _csrf_token(seed: str, settings: Settings) -> str:
    digest = hmac.new(
        settings.csrf_secret_key.encode("utf-8"),
        _CSRF_MAC_DOMAIN + seed.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _valid_csrf_seed(seed: str | None) -> bool:
    """Accept only the exact base64url shape generated by token_urlsafe(32)."""

    return bool(
        seed
        and len(seed) == _CSRF_SEED_LENGTH
        and all(character in _CSRF_SEED_CHARACTERS for character in seed)
    )


def issue_csrf_token(
    response: Response,
    settings: Settings | None = None,
) -> str:
    """Rotate the HttpOnly CSRF seed and return its derived header token."""

    configured = settings or get_settings()
    seed = secrets.token_urlsafe(32)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=seed,
        max_age=SESSION_MAX_AGE_SECONDS,
        expires=SESSION_MAX_AGE_SECONDS,
        path=_cookie_path(configured),
        secure=configured.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return _csrf_token(seed, configured)


def csrf_token_for_request(
    request: Request,
    response: Response,
    settings: Settings | None = None,
) -> str:
    """Return a token for the current seed, creating a seed when absent."""

    configured = settings or get_settings()
    seed = request.cookies.get(CSRF_COOKIE_NAME)
    if not _valid_csrf_seed(seed):
        return issue_csrf_token(response, configured)
    assert seed is not None
    return _csrf_token(seed, configured)


def clear_csrf_cookie(
    response: Response,
    settings: Settings | None = None,
) -> None:
    configured = settings or get_settings()
    response.delete_cookie(
        key=CSRF_COOKIE_NAME,
        path=_cookie_path(configured),
        secure=configured.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def validate_csrf_request(
    request: Request,
    settings: Settings | None = None,
) -> None:
    """Validate CSRF on unsafe methods; CORS preflight and reads pass."""

    if request.method.upper() in _SAFE_METHODS:
        return
    configured = settings or get_settings()
    seed = request.cookies.get(CSRF_COOKIE_NAME)
    supplied = request.headers.get(CSRF_HEADER_NAME)
    if not _valid_csrf_seed(seed) or not supplied:
        raise_csrf_failed()
    assert seed is not None
    try:
        expected = _csrf_token(seed, configured)
    except (UnicodeEncodeError, ValueError):
        raise_csrf_failed()
    if not secrets.compare_digest(expected, supplied):
        raise_csrf_failed()


def authentication_required_exception(
    settings: Settings | None = None,
) -> HTTPException:
    """Build the one generic 401 response and expire a stale session cookie."""

    configured = settings or get_settings()
    temporary_response = Response()
    clear_session_cookie(temporary_response, configured)
    headers = {
        "WWW-Authenticate": "Bearer",
        "Set-Cookie": temporary_response.headers["set-cookie"],
    }
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "authentication_required",
            "message": "Sign in is required.",
        },
        headers=headers,
    )


def raise_csrf_failed() -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "csrf_failed",
            "message": "The request could not be verified.",
        },
    )


def set_google_oauth_cookies(
    response: Response,
    *,
    state: str,
    nonce: str,
    verifier: str,
    settings: Settings | None = None,
) -> None:
    """Store short-lived OAuth correlation secrets in HttpOnly cookies."""

    configured = settings or get_settings()
    for name, value in (
        (GOOGLE_STATE_COOKIE_NAME, state),
        (GOOGLE_NONCE_COOKIE_NAME, nonce),
        (GOOGLE_PKCE_COOKIE_NAME, verifier),
    ):
        response.set_cookie(
            key=name,
            value=value,
            max_age=OAUTH_MAX_AGE_SECONDS,
            expires=OAUTH_MAX_AGE_SECONDS,
            path=_oauth_cookie_path(configured),
            secure=configured.auth_cookie_secure,
            httponly=True,
            samesite="lax",
        )


def clear_google_oauth_cookies(
    response: Response,
    settings: Settings | None = None,
) -> None:
    configured = settings or get_settings()
    for name in (
        GOOGLE_STATE_COOKIE_NAME,
        GOOGLE_NONCE_COOKIE_NAME,
        GOOGLE_PKCE_COOKIE_NAME,
    ):
        response.delete_cookie(
            key=name,
            path=_oauth_cookie_path(configured),
            secure=configured.auth_cookie_secure,
            httponly=True,
            samesite="lax",
        )
