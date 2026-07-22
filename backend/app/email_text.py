"""Utilities for normalizing generated email text."""


def normalize_email_body(value: str) -> str:
    """Return email text with consistent, displayable newline characters."""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return (
        normalized.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
    )
