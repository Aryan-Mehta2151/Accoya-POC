"""Pydantic schemas for browser authentication."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.services.password_service import validate_password


class UserResponse(BaseModel):
    """Safe user details returned to the browser."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    name: str | None
    session_expires_at: datetime


class LoginRequest(BaseModel):
    """Email/password login request."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class ForgotPasswordRequest(BaseModel):
    """Forgot-password request."""

    email: EmailStr


class AccessRequestCreate(BaseModel):
    """Public request to join the approved user list."""

    email: EmailStr
    name: str | None = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ResetPasswordRequest(BaseModel):
    """One-time password reset request."""

    token: str = Field(min_length=32, max_length=512)
    password: str

    @field_validator("password")
    @classmethod
    def password_meets_policy(cls, value: str) -> str:
        return validate_password(value)


class LoginResponse(BaseModel):
    """Cookie-session login response; it deliberately contains no JWT."""

    user: UserResponse
    csrf_token: str


class CsrfResponse(BaseModel):
    """CSRF header material derived from an HttpOnly seed cookie."""

    csrf_token: str


class MessageResponse(BaseModel):
    """Generic authentication operation result."""

    message: str
