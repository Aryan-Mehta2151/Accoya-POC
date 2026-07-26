"""Google OpenID Connect helpers with state, nonce, and PKCE."""

from __future__ import annotations

import hashlib
import secrets
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.services.auth_security import normalize_email


GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


class GoogleOAuthError(RuntimeError):
    """A safe, non-specific Google authentication failure."""


@dataclass(frozen=True)
class GoogleIdentity:
    """Verified account claims used by the application."""

    subject: str
    email: str
    name: str | None


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def google_authorization_url(
    *,
    state: str,
    nonce: str,
    verifier: str,
    settings: Settings,
) -> str:
    """Build the Google authorization URL for an OIDC code flow."""

    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}"


async def exchange_google_code(
    *,
    code: str,
    verifier: str,
    settings: Settings,
) -> str:
    """Exchange one authorization code and return its raw ID token."""

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise GoogleOAuthError("google_token_exchange_failed") from exc

    raw_id_token = payload.get("id_token")
    if not isinstance(raw_id_token, str) or not raw_id_token:
        raise GoogleOAuthError("google_id_token_missing")
    return raw_id_token


async def verify_google_id_token(
    *,
    raw_id_token: str,
    expected_nonce: str,
    settings: Settings,
) -> GoogleIdentity:
    """Verify signature and OIDC claims using Google's official library."""

    try:
        claims: dict[str, Any] = await run_in_threadpool(
            google_id_token.verify_oauth2_token,
            raw_id_token,
            GoogleAuthRequest(),
            settings.google_client_id,
        )
    except (GoogleAuthError, ValueError, TypeError) as exc:
        raise GoogleOAuthError("google_id_token_invalid") from exc

    nonce = claims.get("nonce")
    subject = claims.get("sub")
    email = claims.get("email")
    if (
        not isinstance(nonce, str)
        or not secrets.compare_digest(nonce, expected_nonce)
        or claims.get("email_verified") is not True
        or not isinstance(subject, str)
        or not subject
        or not isinstance(email, str)
        or not email
    ):
        raise GoogleOAuthError("google_identity_invalid")

    name = claims.get("name")
    return GoogleIdentity(
        subject=subject,
        email=normalize_email(email),
        name=name if isinstance(name, str) and name.strip() else None,
    )
