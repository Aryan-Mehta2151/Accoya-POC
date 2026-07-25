"""Pydantic schemas for authentication requests and responses."""

from pydantic import BaseModel, EmailStr


class UserResponse(BaseModel):
    """User details response."""

    id: str
    email: str
    name: str | None


class SignupRequest(BaseModel):
    """Email/password signup request."""

    email: EmailStr
    password: str
    name: str | None = None


class LoginRequest(BaseModel):
    """Email/password login request."""

    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    """Forgot password request."""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Reset password request."""

    token: str
    password: str


class LoginResponse(BaseModel):
    """Login/signup response."""

    access_token: str
    token_type: str
    user: UserResponse


class GoogleOAuthCallbackRequest(BaseModel):
    """Google OAuth callback request from frontend."""

    code: str
