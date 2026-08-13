"""Stable hashing for the editable content of an outreach email."""

from __future__ import annotations

import hashlib
import json


def email_content_hash(
    recipient_email: str | None,
    subject: str,
    body: str,
    signature: str | None = None,
) -> str:
    """Return a deterministic SHA-256 hash for the user-confirmed send payload."""

    payload = {
        "body": render_outreach_body(body, signature),
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


def render_outreach_body(body: str, signature: str | None) -> str:
    """Render the exact plain-text body presented for approval and delivery."""

    rendered_signature = (signature or "").strip()
    if not rendered_signature:
        return body
    return f"{body.strip()}\n\n{rendered_signature}"
