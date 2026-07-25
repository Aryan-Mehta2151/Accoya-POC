"""Authentication routes."""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status, Header
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import PasswordResetToken, User
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    ResetPasswordRequest,
    SignupRequest,
    UserResponse,
)
from app.services.email_service import send_password_reset_email
from app.services.jwt_service import (
    create_access_token,
    create_password_reset_token,
    verify_token,
)
from app.services.oauth_service import get_google_token, get_google_user_info
from app.services.password_service import hash_password, verify_password
from app.config import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


def get_current_user(
    authorization: str | None = Header(None), db: Session = Depends(get_db)
) -> User:
    """Extract user from JWT token in Authorization header."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header",
        )
    
    # Extract token from "Bearer <token>"
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format",
        )
    
    token = parts[1]
    payload = verify_token(token)
    
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    
    stmt = select(User).where(User.id == user_id)
    user = db.execute(stmt).scalars().first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    return user


@router.post("/signup", response_model=LoginResponse)
async def signup(request: SignupRequest, db: Session = Depends(get_db)):
    """Email/password signup."""
    # Check if user already exists
    stmt = select(User).where(User.email == request.email)
    existing_user = db.execute(stmt).scalars().first()
    
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )
    
    # Create new user
    user = User(
        id=str(uuid.uuid4()),
        email=request.email,
        name=request.name,
        password_hash=hash_password(request.password),
    )
    
    db.add(user)
    db.commit()
    db.refresh(user)
    
    # Create access token
    access_token = create_access_token({"sub": user.id, "email": user.email})
    
    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        user=UserResponse(
            id=user.id,
            email=user.email,
            name=user.name,
        ),
    )


@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest, db: Session = Depends(get_db)):
    """Email/password login."""
    # Find user by email
    stmt = select(User).where(User.email == request.email)
    user = db.execute(stmt).scalars().first()
    
    if not user or not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    
    # Verify password
    if not verify_password(request.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    
    # Create access token
    access_token = create_access_token({"sub": user.id, "email": user.email})
    
    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        user=UserResponse(
            id=user.id,
            email=user.email,
            name=user.name,
        ),
    )


@router.get("/callback/google")
async def google_callback(
    code: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    """Handle Google OAuth callback redirect from Google."""
    frontend_url = get_settings().frontend_url

    if error:
        return RedirectResponse(
            url=f"{frontend_url}/auth/callback?error={error}",
            status_code=status.HTTP_302_FOUND,
        )

    if not code:
        return RedirectResponse(
            url=f"{frontend_url}/auth/callback?error=missing_code",
            status_code=status.HTTP_302_FOUND,
        )
    
    # Exchange code for token
    token = await get_google_token(code)
    
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to authenticate with Google",
        )
    
    # Get user info from Google
    user_info = await get_google_user_info(token.get("access_token"))
    
    if not user_info:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to get user info from Google",
        )
    
    email = user_info.get("email")
    oauth_id = user_info.get("id")
    name = user_info.get("name")
    
    if not email or not oauth_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user info from Google",
        )
    
    # Find or create user
    stmt = select(User).where(User.email == email)
    user = db.execute(stmt).scalars().first()
    
    if not user:
        # Create new user
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            name=name,
            oauth_provider="google",
            oauth_id=oauth_id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Update OAuth info if needed
        if not user.oauth_provider:
            user.oauth_provider = "google"
            user.oauth_id = oauth_id
            if not user.name:
                user.name = name
            db.commit()
            db.refresh(user)
    
    # Create access token
    access_token = create_access_token({"sub": user.id, "email": user.email})
    
    # Redirect back to frontend with token
    return RedirectResponse(
        url=f"{frontend_url}/auth/callback?token={access_token}&user={user.id}",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/forgot-password")
async def forgot_password(
    request: ForgotPasswordRequest, db: Session = Depends(get_db)
):
    """Request password reset email."""
    settings = get_settings()
    
    # Find user by email
    stmt = select(User).where(User.email == request.email)
    user = db.execute(stmt).scalars().first()
    
    if not user:
        # For security, don't reveal if email exists
        return {"message": "If email exists, password reset link will be sent"}
    
    # Create password reset token
    token_value = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.password_reset_token_expire_minutes
    )
    
    reset_token = PasswordResetToken(
        id=str(uuid.uuid4()),
        user_id=user.id,
        token=token_value,
        expires_at=expires_at,
    )
    
    db.add(reset_token)
    db.commit()
    
    # Send email
    reset_link = f"{settings.frontend_url}/reset-password?token={token_value}"
    send_password_reset_email(user.email, reset_link)
    
    return {"message": "If email exists, password reset link will be sent"}


@router.post("/reset-password")
async def reset_password(
    request: ResetPasswordRequest, db: Session = Depends(get_db)
):
    """Reset password using token."""
    # Find token
    stmt = select(PasswordResetToken).where(
        PasswordResetToken.token == request.token
    )
    reset_token = db.execute(stmt).scalars().first()
    
    if not reset_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid reset token",
        )
    
    # Check if token expired
    if reset_token.expires_at < datetime.now(timezone.utc):
        db.delete(reset_token)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset token expired",
        )
    
    # Get user and update password
    stmt = select(User).where(User.id == reset_token.user_id)
    user = db.execute(stmt).scalars().first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    user.password_hash = hash_password(request.password)
    db.delete(reset_token)
    db.commit()
    
    return {"message": "Password reset successfully"}


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """Get current user info."""
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        name=current_user.name,
    )
