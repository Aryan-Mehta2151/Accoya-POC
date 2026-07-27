"""Application configuration loaded from environment variables."""
import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import EmailStr
from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _BACKEND_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_PATH), extra="ignore")

    # App
    # Fail closed when APP_ENV is omitted. Only the explicit value
    # "development" relaxes HTTPS/cookie checks and exposes diagnostics.
    app_env: str = "unset"
    api_prefix: str = "/api"
    cors_allowed_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # Database
    database_url: str = (
        "postgresql+psycopg2://postgres:postgres@localhost:5433/accoya_agent"
    )

    # AWS
    aws_region: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    # S3
    s3_bucket_strategy_docs: str = ""

    # Bedrock Knowledge Base
    bedrock_kb_id: str = ""
    bedrock_kb_top_k: int = 5
    # Use the same model ARN configured in the AWS KB test console.
    bedrock_kb_model_arn: str = ""

    # Chatbot
    # Number of prior human/ai turns sent to Gemini as conversation context.
    chat_history_max_turns: int = 10

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_request_timeout_seconds: float = 180.0

    # Durable outreach-generation worker
    email_generation_worker_poll_seconds: float = 2.0
    email_generation_heartbeat_seconds: float = 15.0
    email_generation_stale_seconds: float = 300.0

    # Durable SMTP delivery worker
    email_delivery_worker_poll_seconds: float = 2.0
    email_delivery_heartbeat_seconds: float = 15.0
    email_delivery_stale_seconds: float = 300.0

    # Lead source API (EarlyBid feed)
    lead_api_base_url: str = "https://api.earlybid.bid"
    lead_api_key: str | None = None
    # Default feed to sync: /v1/feeds/{reseller}/{client}/latest.csv
    lead_feed_reseller: str = "amped"
    lead_feed_client: str = "amped-accoya-materials"

    # Separately supervised daily EarlyBid synchronization worker
    lead_auto_sync_timezone: str = "America/Los_Angeles"
    lead_auto_sync_heartbeat_seconds: float = 15.0
    lead_auto_sync_stale_seconds: float = 300.0
    lead_auto_sync_poll_seconds: float = 30.0

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/auth/callback/google"

    # Browser authentication
    jwt_secret_key: str = ""
    csrf_secret_key: str = ""
    jwt_issuer: str = "accoya-api"
    jwt_audience: str = "accoya-web"
    auth_cookie_secure: bool = False
    password_reset_token_expire_minutes: int = 15
    access_request_token_expire_minutes: int = 1440
    access_request_cooldown_minutes: int = 15
    access_request_approver_email: EmailStr = "aryanmehta2151@gmail.com"

    # SMTP
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_email: str = ""
    smtp_password: str = ""

    smtp_timeout_seconds: float = 30.0

    # Frontend URL (for reset password links)
    frontend_url: str = "http://localhost:5173"

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def parse_cors_allowed_origins(cls, value: Any) -> list[str]:
        """Accept a JSON array or a comma-delimited environment value."""

        if isinstance(value, list):
            return value
        if not isinstance(value, str):
            raise ValueError("CORS_ALLOWED_ORIGINS must be a list of origins")

        stripped = value.strip()
        if stripped.startswith("["):
            parsed = json.loads(stripped)
            if not isinstance(parsed, list):
                raise ValueError("CORS_ALLOWED_ORIGINS must be a JSON array")
            return [str(item).strip() for item in parsed]
        return [item.strip() for item in stripped.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
