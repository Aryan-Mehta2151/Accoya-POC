"""Google OAuth service."""

import httpx
from app.config import get_settings


async def get_google_token(code: str) -> dict | None:
    """Exchange Google authorization code for access token."""
    settings = get_settings()
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": "http://localhost:8000/api/auth/callback/google",
                },
            )
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            print(f"Error exchanging Google code: {e}")
            return None


async def get_google_user_info(access_token: str) -> dict | None:
    """Get user info from Google using access token."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code == 200:
                return response.json()
            return None
    except Exception as e:
        print(f"Error getting Google user info: {e}")
        return None

