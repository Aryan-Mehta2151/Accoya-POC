"""Provider-free tests for browser sessions and account administration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import os
import unittest
from base64 import urlsafe_b64encode
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import auth as auth_dependencies
from app.api.routes import auth
from app.auth import admin
from app.config import Settings, get_settings
from app.db.database import Base, get_db
from app.db.models import AccessRequest, AccessRequestStatus, PasswordResetToken, User
from app.services.auth_security import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    hash_password_reset_token,
    validate_web_auth_settings,
)
from app.services.jwt_service import (
    ACCESS_TOKEN_LIFETIME,
    create_access_token,
    verify_access_token,
)
from app.services.oauth_service import GoogleIdentity
from app.services import auth_security, oauth_service
from app.services.oauth_service import GoogleOAuthError
from app.services.password_service import hash_password, verify_password


class AuthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "APP_ENV": "development",
                "API_PREFIX": "/api",
                "JWT_SECRET_KEY": "j" * 40,
                "CSRF_SECRET_KEY": "c" * 40,
                "JWT_ISSUER": "accoya-api",
                "JWT_AUDIENCE": "accoya-web",
                "AUTH_COOKIE_SECURE": "false",
                "FRONTEND_URL": "http://localhost:5173",
                "ACCESS_REQUEST_APPROVER_EMAIL": "aryanmehta2151@gmail.com",
                "ACCESS_REQUEST_TOKEN_EXPIRE_MINUTES": "1440",
                "ACCESS_REQUEST_COOLDOWN_MINUTES": "15",
                "GOOGLE_CLIENT_ID": "test-client",
                "GOOGLE_CLIENT_SECRET": "test-secret",
                "GOOGLE_REDIRECT_URI": (
                    "http://localhost:8000/api/auth/callback/google"
                ),
            },
        )
        self.environment.start()
        get_settings.cache_clear()

        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.app = FastAPI()
        self.app.include_router(auth.router, prefix="/api")

        def override_db():
            with self.sessions() as db:
                yield db

        self.app.dependency_overrides[get_db] = override_db
        self.auth_session_patch = patch.object(
            auth_dependencies,
            "SessionLocal",
            self.sessions,
        )
        self.auth_session_patch.start()
        self.client = TestClient(self.app, base_url="http://localhost")

    def tearDown(self) -> None:
        self.client.close()
        self.auth_session_patch.stop()
        self.engine.dispose()
        get_settings.cache_clear()
        self.environment.stop()

    def _create_user(
        self,
        *,
        email: str = "approved@example.com",
        active: bool = True,
    ) -> User:
        with self.sessions.begin() as db:
            user = User(
                email=email,
                name="Approved User",
                password_hash=hash_password("correct horse battery"),
                is_active=active,
                auth_version=0,
            )
            db.add(user)
            db.flush()
            user_id = user.id
        with self.sessions() as db:
            return db.get(User, user_id)

    def _csrf(self) -> str:
        response = self.client.get("/api/auth/csrf")
        self.assertEqual(response.status_code, 200)
        self.assertIn(CSRF_COOKIE_NAME, self.client.cookies)
        return response.json()["csrf_token"]

    def _login(self, email: str = "approved@example.com"):
        csrf = self._csrf()
        return self.client.post(
            "/api/auth/login",
            headers={CSRF_HEADER_NAME: csrf},
            json={"email": email, "password": "correct horse battery"},
        )

    def test_login_sets_strict_eight_hour_cookie_and_me_uses_database(self) -> None:
        user = self._create_user()

        missing_csrf = self.client.post(
            "/api/auth/login",
            json={
                "email": "approved@example.com",
                "password": "correct horse battery",
            },
        )
        self.assertEqual(missing_csrf.status_code, 403)

        response = self._login(email=" APPROVED@example.com ")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access_token", response.json())
        self.assertEqual(response.json()["user"]["id"], user.id)
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.assertIn("samesite=lax", response.headers["set-cookie"].lower())

        raw_token = self.client.cookies.get(SESSION_COOKIE_NAME)
        claims = jwt.decode(
            raw_token,
            "j" * 40,
            algorithms=["HS256"],
            issuer="accoya-api",
            audience="accoya-web",
        )
        self.assertEqual(claims["token_use"], "access")
        self.assertEqual(claims["auth_version"], 0)
        self.assertEqual(claims["exp"] - claims["iat"], int(ACCESS_TOKEN_LIFETIME.total_seconds()))

        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], "approved@example.com")
        self.assertEqual(
            int(datetime.fromisoformat(me.json()["session_expires_at"]).timestamp()),
            claims["exp"],
        )

    def test_authentication_closes_its_transaction_before_route_work(self) -> None:
        self._create_user()
        logged_in = self._login()
        self.assertEqual(logged_in.status_code, 200)

        observed_sessions = []
        original_factory = self.sessions

        def tracked_session():
            session = original_factory()
            observed_sessions.append(session)
            return session

        with patch.object(
            auth_dependencies,
            "SessionLocal",
            tracked_session,
        ):
            response = self.client.get("/api/auth/me")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(observed_sessions), 1)
        self.assertFalse(observed_sessions[0].in_transaction())
        self.assertIsNone(observed_sessions[0].get_transaction())

    def test_inactive_account_and_stale_auth_version_are_rejected(self) -> None:
        user = self._create_user(active=False)
        denied = self._login()
        self.assertEqual(denied.status_code, 401)

        with self.sessions.begin() as db:
            stored = db.get(User, user.id)
            stored.is_active = True
        accepted = self._login()
        self.assertEqual(accepted.status_code, 200)

        with self.sessions.begin() as db:
            stored = db.get(User, user.id)
            stored.auth_version += 1
        rejected = self.client.get("/api/auth/me")
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(
            rejected.json()["detail"]["code"],
            "authentication_required",
        )

    def test_invalid_session_variants_share_one_generic_401(self) -> None:
        user = self._create_user()
        logged_in = self._login()
        self.assertEqual(logged_in.status_code, 200)
        valid_token = self.client.cookies.get(SESSION_COOKIE_NAME)
        claims = jwt.get_unverified_claims(valid_token)

        missing_sub_claims = dict(claims)
        missing_sub_claims.pop("sub")
        missing_sub = jwt.encode(
            missing_sub_claims,
            "j" * 40,
            algorithm="HS256",
        )
        malformed_sub = jwt.encode(
            {**claims, "sub": "not-a-uuid"},
            "j" * 40,
            algorithm="HS256",
        )
        expired = create_access_token(
            user_id=user.id,
            auth_version=0,
            settings=get_settings(),
            now=datetime.now(timezone.utc) - timedelta(hours=9),
        )

        with self.sessions.begin() as db:
            db.delete(db.get(User, user.id))

        expected = {
            "detail": {
                "code": "authentication_required",
                "message": "Sign in is required.",
            }
        }
        variants = (
            ("missing", None),
            ("expired", expired),
            ("missing_sub", missing_sub),
            ("malformed_sub", malformed_sub),
            ("deleted_user", valid_token),
        )
        for label, token in variants:
            with self.subTest(label=label):
                self.client.cookies.clear()
                if token is not None:
                    self.client.cookies.set(
                        SESSION_COOKIE_NAME,
                        token,
                        path="/api",
                    )
                response = self.client.get("/api/auth/me")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), expected)
                self.assertEqual(
                    response.headers["www-authenticate"],
                    "Bearer",
                )
                self.assertIn(
                    f"{SESSION_COOKIE_NAME}=",
                    response.headers["set-cookie"],
                )

    def test_public_password_routes_reject_missing_or_wrong_csrf(self) -> None:
        reset_email = patch.object(auth, "send_password_reset_email")
        with reset_email as send_reset:
            missing = self.client.post(
                "/api/auth/forgot-password",
                json={"email": "approved@example.com"},
            )
        self.assertEqual(missing.status_code, 403)
        send_reset.assert_not_called()

        self._csrf()
        wrong = self.client.post(
            "/api/auth/reset-password",
            headers={"X-CSRF-Token": "wrong-token"},
            json={
                "token": "r" * 32,
                "password": "a new secure password",
            },
        )
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(wrong.json()["detail"]["code"], "csrf_failed")

    def test_malformed_caller_supplied_csrf_seed_is_rotated(self) -> None:
        attacker_seed = "attacker-controlled-signing-input"
        self.client.cookies.set(
            CSRF_COOKIE_NAME,
            attacker_seed,
            path="/api",
        )

        response = self.client.get("/api/auth/csrf")

        self.assertEqual(response.status_code, 200)
        rotated_seed = response.cookies.get(CSRF_COOKIE_NAME)
        self.assertNotEqual(rotated_seed, attacker_seed)
        self.assertEqual(len(rotated_seed), 43)
        self.assertTrue(
            all(
                character.isalnum() or character in "-_"
                for character in rotated_seed
            )
        )

    def test_password_reset_stores_only_hash_and_revokes_sessions(self) -> None:
        user = self._create_user()
        csrf = self._csrf()
        reset_link: list[str] = []
        with patch.object(
            auth,
            "send_password_reset_email",
            side_effect=lambda _email, link: reset_link.append(link) or True,
        ):
            response = self.client.post(
                "/api/auth/forgot-password",
                headers={CSRF_HEADER_NAME: csrf},
                json={"email": user.email},
            )
        self.assertEqual(response.status_code, 200)
        reset_url = urlparse(reset_link[0])
        self.assertEqual(reset_url.query, "")
        raw_token = parse_qs(reset_url.fragment)["token"][0]

        with self.sessions() as db:
            record = db.scalar(select(PasswordResetToken))
            self.assertEqual(record.token_hash, hash_password_reset_token(raw_token))
            self.assertNotEqual(record.token_hash, raw_token)

        reset = self.client.post(
            "/api/auth/reset-password",
            headers={CSRF_HEADER_NAME: csrf},
            json={"token": raw_token, "password": "a new secure password"},
        )
        self.assertEqual(reset.status_code, 200)
        repeated = self.client.post(
            "/api/auth/reset-password",
            headers={CSRF_HEADER_NAME: csrf},
            json={"token": raw_token, "password": "another secure password"},
        )
        self.assertEqual(repeated.status_code, 400)

        with self.sessions() as db:
            stored = db.get(User, user.id)
            self.assertEqual(stored.auth_version, 1)
            self.assertTrue(
                verify_password("a new secure password", stored.password_hash)
            )
            self.assertIsNone(db.scalar(select(PasswordResetToken)))

    def test_password_reset_delivery_failure_is_logged_without_identity(self) -> None:
        self._create_user()
        csrf = self._csrf()

        with (
            patch.object(auth, "send_password_reset_email", return_value=False),
            self.assertLogs(auth.__name__, level="ERROR") as captured,
        ):
            response = self.client.post(
                "/api/auth/forgot-password",
                headers={CSRF_HEADER_NAME: csrf},
                json={"email": "approved@example.com"},
            )

        self.assertEqual(response.status_code, 200)
        rendered = "\n".join(captured.output)
        self.assertIn("Password reset email delivery failed", rendered)
        self.assertNotIn("approved@example.com", rendered)

    def test_forgot_password_closes_transaction_before_mail_delivery(self) -> None:
        user = self._create_user()
        csrf = self._csrf()
        expiring_sessions = sessionmaker(bind=self.engine)
        observed_sessions = []
        mail_transaction_states: list[bool] = []

        def override_db():
            with expiring_sessions() as db:
                observed_sessions.append(db)
                yield db

        def observe_mail_delivery(_email: str, _link: str) -> bool:
            mail_transaction_states.append(observed_sessions[0].in_transaction())
            return True

        original_override = self.app.dependency_overrides[get_db]
        self.app.dependency_overrides[get_db] = override_db
        try:
            with patch.object(
                auth,
                "send_password_reset_email",
                side_effect=observe_mail_delivery,
            ):
                response = self.client.post(
                    "/api/auth/forgot-password",
                    headers={CSRF_HEADER_NAME: csrf},
                    json={"email": user.email},
                )
        finally:
            self.app.dependency_overrides[get_db] = original_override

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mail_transaction_states, [False])

    def test_request_access_requires_csrf_and_sends_approver_email(self) -> None:
        missing = self.client.post(
            "/api/auth/request-access",
            json={"email": "new.user@example.com", "name": "New User"},
        )
        self.assertEqual(missing.status_code, 403)

        csrf = self._csrf()
        captured: dict[str, str] = {}

        def _capture_review_email(**kwargs) -> bool:
            captured.update({k: str(v) for k, v in kwargs.items()})
            return True

        with patch.object(
            auth,
            "send_access_request_review_email",
            side_effect=_capture_review_email,
        ):
            response = self.client.post(
                "/api/auth/request-access",
                headers={CSRF_HEADER_NAME: csrf},
                json={"email": " NEW.USER@Example.com ", "name": " New User "},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["message"],
            "If access can be granted, the request will be reviewed shortly.",
        )
        self.assertEqual(captured["approver_email"], "aryanmehta2151@gmail.com")
        self.assertEqual(captured["requester_email"], "new.user@example.com")
        self.assertIn("decision=approve", captured["approve_link"])
        self.assertIn("decision=reject", captured["reject_link"])

        with self.sessions() as db:
            record = db.scalar(select(AccessRequest))
            self.assertIsNotNone(record)
            self.assertEqual(record.email, "new.user@example.com")
            self.assertEqual(record.status, AccessRequestStatus.pending)
            self.assertEqual(len(record.token_hash), 64)

    def test_request_access_resubmission_rotates_pending_token(self) -> None:
        csrf = self._csrf()
        first_email_call_count = 0

        def _send_review_email(**_kwargs) -> bool:
            nonlocal first_email_call_count
            first_email_call_count += 1
            return True

        with patch.object(
            auth,
            "send_access_request_review_email",
            side_effect=_send_review_email,
        ):
            first = self.client.post(
                "/api/auth/request-access",
                headers={CSRF_HEADER_NAME: csrf},
                json={"email": "repeat@example.com", "name": "First"},
            )
            self.assertEqual(first.status_code, 200)

        with self.sessions() as db:
            original = db.scalar(select(AccessRequest))
            first_hash = original.token_hash
            original.requested_at = datetime.now(timezone.utc) - timedelta(minutes=20)
            db.commit()

        with patch.object(
            auth,
            "send_access_request_review_email",
            side_effect=_send_review_email,
        ):
            second = self.client.post(
                "/api/auth/request-access",
                headers={CSRF_HEADER_NAME: csrf},
                json={"email": "repeat@example.com", "name": "Second"},
            )
            self.assertEqual(second.status_code, 200)

        with self.sessions() as db:
            rows = db.scalars(select(AccessRequest)).all()
            self.assertEqual(len(rows), 1)
            self.assertNotEqual(rows[0].token_hash, first_hash)
            self.assertEqual(rows[0].name, "Second")
        self.assertEqual(first_email_call_count, 2)

    def test_request_access_resubmission_within_cooldown_is_blocked(self) -> None:
        csrf = self._csrf()

        with patch.object(
            auth,
            "send_access_request_review_email",
            return_value=True,
        ) as send_review:
            first = self.client.post(
                "/api/auth/request-access",
                headers={CSRF_HEADER_NAME: csrf},
                json={"email": "cooldown@example.com", "name": "First"},
            )
            self.assertEqual(first.status_code, 200)

            with self.sessions() as db:
                original = db.scalar(
                    select(AccessRequest).where(
                        AccessRequest.email == "cooldown@example.com"
                    )
                )
                first_hash = original.token_hash

            second = self.client.post(
                "/api/auth/request-access",
                headers={CSRF_HEADER_NAME: csrf},
                json={"email": "cooldown@example.com", "name": "Second"},
            )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            second.json()["message"],
            "A request was already submitted recently. Please wait before trying again.",
        )
        self.assertEqual(send_review.call_count, 1)

        with self.sessions() as db:
            current = db.scalar(
                select(AccessRequest).where(
                    AccessRequest.email == "cooldown@example.com"
                )
            )
            self.assertEqual(current.name, "First")
            self.assertEqual(current.token_hash, first_hash)

    def test_request_access_returns_already_has_access_for_active_user(self) -> None:
        self._create_user(email="already.active@example.com", active=True)
        csrf = self._csrf()

        with patch.object(
            auth,
            "send_access_request_review_email",
            return_value=True,
        ) as send_review:
            response = self.client.post(
                "/api/auth/request-access",
                headers={CSRF_HEADER_NAME: csrf},
                json={"email": "already.active@example.com", "name": "Existing User"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["message"],
            "This email already has access. Please sign in or use Forgot Password.",
        )
        send_review.assert_not_called()

        with self.sessions() as db:
            pending = db.scalars(
                select(AccessRequest).where(
                    AccessRequest.email == "already.active@example.com"
                )
            ).all()
            self.assertEqual(pending, [])

    def test_review_access_request_approve_creates_active_user_and_reset_token(self) -> None:
        csrf = self._csrf()
        links: dict[str, str] = {}

        def _capture_review_email(**kwargs) -> bool:
            links["approve"] = kwargs["approve_link"]
            return True

        with patch.object(
            auth,
            "send_access_request_review_email",
            side_effect=_capture_review_email,
        ):
            requested = self.client.post(
                "/api/auth/request-access",
                headers={CSRF_HEADER_NAME: csrf},
                json={"email": "approved.request@example.com", "name": "Requester"},
            )
        self.assertEqual(requested.status_code, 200)

        decision_calls: list[dict[str, str]] = []

        def _capture_decision_email(**kwargs) -> bool:
            decision_calls.append({k: str(v) for k, v in kwargs.items()})
            return True

        with patch.object(
            auth,
            "send_access_request_decision_email",
            side_effect=_capture_decision_email,
        ):
            review = self.client.get(links["approve"])

        self.assertEqual(review.status_code, 200)
        self.assertIn("Access Request Approved", review.text)
        self.assertIn("password setup email has been sent", review.text)
        self.assertEqual(len(decision_calls), 1)
        self.assertEqual(decision_calls[0]["approved"], "True")
        self.assertIn("reset-password#token=", decision_calls[0]["reset_link"])

        with self.sessions() as db:
            user = db.scalar(
                select(User).where(User.email == "approved.request@example.com")
            )
            self.assertIsNotNone(user)
            self.assertTrue(user.is_active)
            request_record = db.scalar(
                select(AccessRequest).where(
                    AccessRequest.email == "approved.request@example.com"
                )
            )
            self.assertEqual(request_record.status, AccessRequestStatus.approved)
            self.assertIsNone(request_record.token_hash)
            reset = db.scalar(
                select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
            )
            self.assertIsNotNone(reset)

    def test_review_access_request_reject_marks_terminal_state(self) -> None:
        csrf = self._csrf()
        links: dict[str, str] = {}

        def _capture_review_email(**kwargs) -> bool:
            links["reject"] = kwargs["reject_link"]
            return True

        with patch.object(
            auth,
            "send_access_request_review_email",
            side_effect=_capture_review_email,
        ):
            requested = self.client.post(
                "/api/auth/request-access",
                headers={CSRF_HEADER_NAME: csrf},
                json={"email": "reject.request@example.com"},
            )
        self.assertEqual(requested.status_code, 200)

        with patch.object(
            auth,
            "send_access_request_decision_email",
            return_value=True,
        ):
            review = self.client.get(links["reject"])
        self.assertEqual(review.status_code, 200)
        self.assertIn("Access Request Rejected", review.text)
        self.assertIn("No account changes were applied", review.text)

        with self.sessions() as db:
            request_record = db.scalar(
                select(AccessRequest).where(
                    AccessRequest.email == "reject.request@example.com"
                )
            )
            self.assertEqual(request_record.status, AccessRequestStatus.rejected)
            self.assertIsNone(request_record.token_hash)
            user = db.scalar(
                select(User).where(User.email == "reject.request@example.com")
            )
            self.assertIsNone(user)

    def test_review_access_request_fails_after_expiry(self) -> None:
        with self.sessions.begin() as db:
            db.add(
                AccessRequest(
                    email="expired.request@example.com",
                    status=AccessRequestStatus.pending,
                    token_hash=hash_password_reset_token("x" * 32),
                    expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                    requested_at=datetime.now(timezone.utc),
                )
            )

        response = self.client.get(
            "/api/auth/access-requests/review",
            params={"token": "x" * 32, "decision": "approve"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("This access review link is no longer valid", response.text)
        self.assertIn("Access Review Link Invalid", response.text)

        with self.sessions() as db:
            request_record = db.scalar(
                select(AccessRequest).where(
                    AccessRequest.email == "expired.request@example.com"
                )
            )
            self.assertEqual(request_record.status, AccessRequestStatus.expired)
            self.assertIsNone(request_record.token_hash)

    def test_logout_requires_csrf_clears_cookies_and_revokes_old_token(self) -> None:
        user = self._create_user()
        logged_in = self._login()
        self.assertEqual(logged_in.status_code, 200)
        old_token = self.client.cookies.get(SESSION_COOKIE_NAME)

        missing_csrf = self.client.post("/api/auth/logout")
        self.assertEqual(missing_csrf.status_code, 403)
        signed_out = self.client.post(
            "/api/auth/logout",
            headers={CSRF_HEADER_NAME: logged_in.json()["csrf_token"]},
        )
        self.assertEqual(signed_out.status_code, 200)
        self.assertNotIn(SESSION_COOKIE_NAME, self.client.cookies)
        self.assertNotIn(CSRF_COOKIE_NAME, self.client.cookies)

        with self.sessions() as db:
            self.assertEqual(db.get(User, user.id).auth_version, 1)
        self.client.cookies.set(
            SESSION_COOKIE_NAME,
            old_token,
            path="/api",
        )
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_google_login_links_only_an_active_preapproved_account(self) -> None:
        user = self._create_user()
        started = self.client.get("/api/auth/google/start", follow_redirects=False)
        self.assertEqual(started.status_code, 302)
        query = parse_qs(urlparse(started.headers["location"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])

        with (
            patch.object(
                auth,
                "exchange_google_code",
                new=AsyncMock(return_value="verified-id-token"),
            ),
            patch.object(
                auth,
                "verify_google_id_token",
                new=AsyncMock(
                    return_value=GoogleIdentity(
                        subject="google-subject",
                        email=user.email,
                        name="Google Name",
                    )
                ),
            ),
        ):
            callback = self.client.get(
                "/api/auth/callback/google",
                params={"code": "code", "state": query["state"][0]},
                follow_redirects=False,
            )
        self.assertEqual(callback.status_code, 302)
        self.assertEqual(
            callback.headers["location"],
            "http://localhost:5173/auth/callback",
        )
        self.assertIn(SESSION_COOKIE_NAME, self.client.cookies)
        with self.sessions() as db:
            stored = db.get(User, user.id)
            self.assertEqual(stored.oauth_provider, "google")
            self.assertEqual(stored.oauth_id, "google-subject")

        self.client.cookies.clear()
        started = self.client.get("/api/auth/google/start", follow_redirects=False)
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        with (
            patch.object(
                auth,
                "exchange_google_code",
                new=AsyncMock(return_value="verified-id-token"),
            ),
            patch.object(
                auth,
                "verify_google_id_token",
                new=AsyncMock(
                    return_value=GoogleIdentity(
                        subject="unknown-subject",
                        email="not-approved@example.com",
                        name=None,
                    )
                ),
            ),
        ):
            callback = self.client.get(
                "/api/auth/callback/google",
                params={"code": "code", "state": state},
                follow_redirects=False,
            )
        self.assertEqual(callback.status_code, 302)
        self.assertEqual(
            callback.headers["location"],
            "http://localhost:5173/auth/callback?error=access_not_approved",
        )
        with self.sessions() as db:
            self.assertIsNone(
                db.scalar(select(User).where(User.email == "not-approved@example.com"))
            )

    def test_google_callback_rejects_wrong_state_before_code_exchange(self) -> None:
        self._create_user()
        started = self.client.get("/api/auth/google/start", follow_redirects=False)
        self.assertEqual(started.status_code, 302)

        exchange = AsyncMock(return_value="must-not-be-used")
        with patch.object(auth, "exchange_google_code", new=exchange):
            callback = self.client.get(
                "/api/auth/callback/google",
                params={"code": "code", "state": "wrong-state"},
                follow_redirects=False,
            )

        self.assertEqual(callback.status_code, 302)
        self.assertEqual(
            callback.headers["location"],
            "http://localhost:5173/auth/callback?error=oauth_failed",
        )
        exchange.assert_not_awaited()
        self.assertNotIn(SESSION_COOKIE_NAME, self.client.cookies)

    def test_google_callback_rejects_an_identity_linked_to_another_user(self) -> None:
        approved = self._create_user()
        with self.sessions.begin() as db:
            db.add(
                User(
                    email="identity-owner@example.com",
                    name="Identity Owner",
                    is_active=True,
                    auth_version=0,
                    oauth_provider="google",
                    oauth_id="shared-google-subject",
                )
            )

        started = self.client.get("/api/auth/google/start", follow_redirects=False)
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        with (
            patch.object(
                auth,
                "exchange_google_code",
                new=AsyncMock(return_value="verified-id-token"),
            ),
            patch.object(
                auth,
                "verify_google_id_token",
                new=AsyncMock(
                    return_value=GoogleIdentity(
                        subject="shared-google-subject",
                        email=approved.email,
                        name=None,
                    )
                ),
            ),
        ):
            callback = self.client.get(
                "/api/auth/callback/google",
                params={"code": "code", "state": state},
                follow_redirects=False,
            )

        self.assertEqual(callback.status_code, 302)
        self.assertEqual(
            callback.headers["location"],
            "http://localhost:5173/auth/callback?error=oauth_failed",
        )
        self.assertNotIn(SESSION_COOKIE_NAME, self.client.cookies)
        with self.sessions() as db:
            stored = db.get(User, approved.id)
            self.assertIsNone(stored.oauth_provider)
            self.assertIsNone(stored.oauth_id)


class AuthConfigurationAndAdminTests(unittest.TestCase):
    def test_google_id_token_requires_matching_nonce_and_verified_email(self):
        settings = Settings(google_client_id="client-id")
        base_claims = {
            "nonce": "expected-nonce",
            "sub": "google-subject",
            "email": " APPROVED@Example.com ",
            "email_verified": True,
            "name": "Approved User",
        }

        with patch.object(
            oauth_service.google_id_token,
            "verify_oauth2_token",
            return_value=base_claims,
        ):
            identity = asyncio.run(
                oauth_service.verify_google_id_token(
                    raw_id_token="signed-token",
                    expected_nonce="expected-nonce",
                    settings=settings,
                )
            )
        self.assertEqual(identity.email, "approved@example.com")

        for changed in (
            {**base_claims, "nonce": "wrong-nonce"},
            {**base_claims, "email_verified": False},
        ):
            with (
                self.subTest(claims=changed),
                patch.object(
                    oauth_service.google_id_token,
                    "verify_oauth2_token",
                    return_value=changed,
                ),
                self.assertRaises(GoogleOAuthError),
            ):
                asyncio.run(
                    oauth_service.verify_google_id_token(
                        raw_id_token="signed-token",
                        expected_nonce="expected-nonce",
                        settings=settings,
                    )
                )

    def test_access_tokens_reject_wrong_purpose_issuer_audience_and_version_type(self):
        settings = Settings(
            jwt_secret_key="j" * 40,
            csrf_secret_key="c" * 40,
            jwt_issuer="accoya-api",
            jwt_audience="accoya-web",
        )
        token = create_access_token(
            user_id="c792abce-e52a-4e9d-b93a-a826837b9320",
            auth_version=3,
            settings=settings,
        )
        self.assertIsNotNone(verify_access_token(token, settings=settings))
        claims = jwt.get_unverified_claims(token)

        for key, value in (
            ("token_use", "password_reset"),
            ("iss", "another-api"),
            ("aud", "another-client"),
            ("auth_version", "3"),
            ("exp", claims["exp"] + 60),
        ):
            with self.subTest(claim=key):
                changed = {**claims, key: value}
                altered = jwt.encode(changed, "j" * 40, algorithm="HS256")
                self.assertIsNone(
                    verify_access_token(altered, settings=settings)
                )

        wrong_signature = jwt.encode(claims, "x" * 40, algorithm="HS256")
        self.assertIsNone(verify_access_token(wrong_signature, settings=settings))

    def test_security_configuration_rejects_weak_keys_and_insecure_production(self):
        self.assertEqual(Settings.model_fields["app_env"].default, "unset")
        with self.assertRaisesRegex(RuntimeError, "JWT_SECRET_KEY"):
            validate_web_auth_settings(Settings(jwt_secret_key="", csrf_secret_key="c" * 32))

        production = Settings(
            app_env="production",
            jwt_secret_key="j" * 32,
            csrf_secret_key="c" * 32,
            auth_cookie_secure=False,
            frontend_url="http://example.com",
            google_redirect_uri="http://api.example.com/api/auth/callback/google",
            cors_allowed_origins=["http://example.com"],
        )
        with self.assertRaisesRegex(RuntimeError, "AUTH_COOKIE_SECURE"):
            validate_web_auth_settings(production)

        staging = production.model_copy(
            update={
                "app_env": "staging",
                "auth_cookie_secure": False,
            }
        )
        with self.assertRaisesRegex(RuntimeError, "AUTH_COOKIE_SECURE"):
            validate_web_auth_settings(staging)

        incomplete_production = Settings(
            app_env="production",
            jwt_secret_key="j" * 32,
            csrf_secret_key="c" * 32,
            auth_cookie_secure=True,
            frontend_url="https://app.example.com",
            google_redirect_uri=(
                "https://app.example.com/api/auth/callback/google"
            ),
            google_client_id="",
            google_client_secret="",
            cors_allowed_origins=["https://app.example.com"],
            microsoft_client_id="",
            microsoft_tenant_id="",
            microsoft_client_secret="",
        )
        with self.assertRaisesRegex(RuntimeError, "Google credentials"):
            validate_web_auth_settings(incomplete_production)

        with self.assertRaisesRegex(RuntimeError, "configured together"):
            validate_web_auth_settings(
                Settings(
                    jwt_secret_key="j" * 32,
                    csrf_secret_key="c" * 32,
                    google_client_id="only-a-client-id",
                    google_client_secret="",
                )
            )

        cross_site = Settings(
            app_env="production",
            jwt_secret_key="j" * 32,
            csrf_secret_key="c" * 32,
            auth_cookie_secure=True,
            frontend_url="https://app.example.com",
            google_redirect_uri=(
                "https://api.example.com/api/auth/callback/google"
            ),
            google_client_id="production-client",
            google_client_secret="production-secret",
            cors_allowed_origins=["https://app.example.com"],
            microsoft_client_id="production-client",
            microsoft_tenant_id="production-tenant",
            microsoft_client_secret="production-secret",
            microsoft_sender_email="mailer@example.com",
        )
        with self.assertRaisesRegex(RuntimeError, "same hostname"):
            validate_web_auth_settings(cross_site)

        validate_web_auth_settings(
            cross_site.model_copy(
                update={
                    "google_redirect_uri": (
                        "https://app.example.com/api/auth/callback/google"
                    ),
                }
            )
        )

        with self.assertRaisesRegex(RuntimeError, "Invalid CORS origin"):
            validate_web_auth_settings(
                Settings(
                    jwt_secret_key="j" * 32,
                    csrf_secret_key="c" * 32,
                    cors_allowed_origins=["http://localhost:5173/"],
                )
            )

        with self.assertRaisesRegex(RuntimeError, "exactly target"):
            validate_web_auth_settings(
                Settings(
                    jwt_secret_key="j" * 32,
                    csrf_secret_key="c" * 32,
                    google_redirect_uri="http://localhost:8000/wrong/callback",
                )
            )

        with self.assertRaisesRegex(RuntimeError, "PASSWORD_RESET"):
            validate_web_auth_settings(
                Settings(
                    jwt_secret_key="j" * 32,
                    csrf_secret_key="c" * 32,
                    password_reset_token_expire_minutes=0,
                )
            )

    def test_jwt_and_csrf_secrets_must_differ_and_csrf_mac_is_domain_bound(self):
        shared_secret = "s" * 40
        settings = Settings(
            jwt_secret_key=shared_secret,
            csrf_secret_key=shared_secret,
        )
        with self.assertRaisesRegex(RuntimeError, "must be different"):
            validate_web_auth_settings(settings)

        seed = "A" * 43
        legacy_hs256_oracle = urlsafe_b64encode(
            hmac.new(
                shared_secret.encode("utf-8"),
                seed.encode("ascii"),
                hashlib.sha256,
            ).digest()
        ).rstrip(b"=").decode("ascii")
        csrf_mac = auth_security._csrf_token(seed, settings)
        self.assertNotEqual(csrf_mac, legacy_hs256_oracle)

    def test_admin_cli_creates_disables_and_revokes_without_password_arguments(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with (
                patch.object(admin, "SessionLocal", sessions),
                patch.object(
                    admin.getpass,
                    "getpass",
                    side_effect=["correct horse battery", "correct horse battery"],
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = admin.main(
                    [
                        "create",
                        "--email",
                        " APPROVED@Example.com ",
                        "--name",
                        "Approved User",
                    ]
                )
            self.assertEqual(result, 0)
            with sessions() as db:
                user = db.scalar(select(User))
                self.assertEqual(user.email, "approved@example.com")
                self.assertTrue(user.is_active)
                self.assertTrue(
                    verify_password("correct horse battery", user.password_hash)
                )
                user_id = user.id

            with sessions.begin() as db:
                db.add(
                    PasswordResetToken(
                        user_id=user_id,
                        token_hash="b" * 64,
                        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    )
                )

            with patch.object(admin, "SessionLocal", sessions):
                self.assertEqual(
                    admin.main(["disable", "--email", "approved@example.com"]),
                    0,
                )

            with sessions() as db:
                user = db.scalar(select(User))
                self.assertFalse(user.is_active)
                self.assertEqual(user.auth_version, 1)
                self.assertIsNone(db.scalar(select(PasswordResetToken)))

            with patch.object(admin, "SessionLocal", sessions):
                self.assertEqual(
                    admin.main(
                        ["revoke-sessions", "--email", "approved@example.com"]
                    ),
                    0,
                )
            with sessions() as db:
                user = db.scalar(select(User))
                self.assertFalse(user.is_active)
                self.assertEqual(user.auth_version, 2)
        finally:
            engine.dispose()

    def test_admin_cli_lists_enables_and_sets_password_with_expected_errors(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with sessions.begin() as db:
                user = User(
                    email="approved@example.com",
                    name="Approved User",
                    password_hash=hash_password("correct horse battery"),
                    is_active=False,
                    auth_version=0,
                )
                db.add(user)
                db.flush()
                db.add(
                    PasswordResetToken(
                        user_id=user.id,
                        token_hash="a" * 64,
                        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    )
                )

            with (
                patch.object(admin, "SessionLocal", sessions),
                patch.object(
                    admin.getpass,
                    "getpass",
                    side_effect=["a replacement password", "a replacement password"],
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                self.assertEqual(admin.main(["list"]), 0)
                self.assertEqual(
                    admin.main(["enable", "--email", "approved@example.com"]),
                    0,
                )
                self.assertEqual(
                    admin.main(
                        ["set-password", "--email", "approved@example.com"]
                    ),
                    0,
                )
                self.assertEqual(
                    admin.main(["disable", "--email", "missing@example.com"]),
                    2,
                )

            self.assertIn("approved@example.com", stdout.getvalue())
            self.assertIn("password", stdout.getvalue())
            self.assertIn("No user exists", stderr.getvalue())
            with sessions() as db:
                stored = db.scalar(select(User))
                self.assertTrue(stored.is_active)
                self.assertEqual(stored.auth_version, 1)
                self.assertTrue(
                    verify_password(
                        "a replacement password",
                        stored.password_hash,
                    )
                )
                self.assertIsNone(db.scalar(select(PasswordResetToken)))
        finally:
            engine.dispose()

    def test_admin_cli_creates_google_only_without_prompting_for_a_password(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        password_prompt = patch.object(admin.getpass, "getpass")
        try:
            with (
                patch.object(admin, "SessionLocal", sessions),
                password_prompt as get_password,
                redirect_stdout(io.StringIO()),
            ):
                result = admin.main(
                    [
                        "create",
                        "--email",
                        "google@example.com",
                        "--google-only",
                    ]
                )

            self.assertEqual(result, 0)
            get_password.assert_not_called()
            with sessions() as db:
                user = db.scalar(select(User))
                self.assertTrue(user.is_active)
                self.assertIsNone(user.password_hash)
                self.assertIsNone(user.oauth_provider)
                self.assertIsNone(user.oauth_id)
        finally:
            engine.dispose()

    def test_admin_cli_sanitizes_database_conflict_details(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        stderr = io.StringIO()
        sensitive_hash = "$2b$12$sensitive-bcrypt-material"
        database_error = IntegrityError(
            "INSERT INTO users (email, password_hash) VALUES (...) ",
            {"email": "private@example.com", "password_hash": sensitive_hash},
            RuntimeError("duplicate"),
        )
        try:
            with (
                patch.object(admin, "SessionLocal", sessions),
                patch.object(admin, "_create", side_effect=database_error),
                redirect_stderr(stderr),
            ):
                result = admin.main(
                    ["create", "--email", "private@example.com"]
                )

            self.assertEqual(result, 2)
            rendered = stderr.getvalue()
            self.assertIn("conflicted with current database state", rendered)
            self.assertNotIn("private@example.com", rendered)
            self.assertNotIn(sensitive_hash, rendered)
            self.assertNotIn("INSERT INTO", rendered)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
