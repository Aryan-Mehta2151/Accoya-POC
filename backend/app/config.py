"""Application configuration loaded from environment variables."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _BACKEND_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_PATH), extra="ignore")

    # App
    app_env: str = "development"
    api_prefix: str = "/api"

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

    # Lead source API (EarlyBid feed)
    lead_api_base_url: str = "https://api.earlybid.bid"
    lead_api_key: str | None = None
    # Default feed to sync: /v1/feeds/{reseller}/{client}/latest.csv
    lead_feed_reseller: str = "amped"
    lead_feed_client: str = "amped-accoya-materials"


@lru_cache
def get_settings() -> Settings:
    return Settings()
