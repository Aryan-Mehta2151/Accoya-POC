"""Shared boto3 session/clients configured from settings."""
import boto3

from app.config import get_settings

settings = get_settings()


def _session() -> boto3.Session:
    # When keys are empty, boto3 falls back to the instance/task IAM role.
    return boto3.Session(
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
    )


def s3_client():
    return _session().client("s3")


def bedrock_agent_runtime_client():
    return _session().client("bedrock-agent-runtime")
