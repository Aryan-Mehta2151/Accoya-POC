"""S3 helpers for storing strategy documents."""
from datetime import datetime

from app.config import get_settings
from app.services.aws import s3_client

settings = get_settings()


def upload_strategy_doc(file_bytes: bytes, key: str, content_type: str | None = None) -> str:
    """Upload a strategy document to S3 and return its key."""
    client = s3_client()
    extra = {"ContentType": content_type} if content_type else {}
    client.put_object(
        Bucket=settings.s3_bucket_strategy_docs,
        Key=key,
        Body=file_bytes,
        **extra,
    )
    return key


def presigned_url(key: str, expires_in: int = 3600) -> str:
    client = s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket_strategy_docs, "Key": key},
        ExpiresIn=expires_in,
    )


def list_strategy_docs(prefix: str | None = None) -> list[dict]:
    """List documents currently in the configured S3 bucket.

    If `prefix` is None, returns all objects in the bucket.
    """
    client = s3_client()
    paginator = client.get_paginator("list_objects_v2")

    kwargs = {"Bucket": settings.s3_bucket_strategy_docs}
    if prefix:
        kwargs["Prefix"] = prefix

    docs: list[dict] = []
    for page in paginator.paginate(**kwargs):
        for item in page.get("Contents", []):
            key = item["Key"]
            docs.append(
                {
                    "id": key,
                    "s3_key": key,
                    "filename": key.split("/")[-1],
                    "size": item.get("Size", 0),
                    "last_modified": _to_iso(item.get("LastModified")),
                    "url": presigned_url(key),
                }
            )

    docs.sort(key=lambda d: d.get("last_modified") or "", reverse=True)
    return docs


def delete_strategy_doc(key: str) -> None:
    """Delete a strategy document from S3 by key."""
    client = s3_client()
    client.delete_object(Bucket=settings.s3_bucket_strategy_docs, Key=key)


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()
