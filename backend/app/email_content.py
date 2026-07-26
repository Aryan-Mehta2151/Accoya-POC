"""Stable hashing for the editable content of an outreach email."""

from __future__ import annotations

import hashlib
import json


def email_content_hash(
    recipient_email: str | None,
    subject: str,
    body: str,
) -> str:
    """Return a deterministic SHA-256 hash for the user-confirmed send payload."""

    payload = {
        "body": body,
        "recipient_email": recipient_email,
        "subject": subject,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
