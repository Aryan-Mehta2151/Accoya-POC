"""Plain-text signature policy for outreach emails."""

from __future__ import annotations

from agent.normalization import normalize_state_code


DEFAULT_US_EMAIL_SIGNATURE = """Doug Gillikin
Specification Manager (Associate AIA)
Accsys

Accsys Sales Office
Accoya USA
Building 470,
200 S Wilcox Dr.
Kingsport, TN
37660-5147"""

US_STATE_CODES = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC",
    }
)


def is_us_opportunity_state(value: object) -> bool:
    """Return whether a normalized or source state identifies the US market."""

    return normalize_state_code(value) in US_STATE_CODES


def default_signature_for_state(value: object) -> str | None:
    """Return the fixed US signature only for explicitly recognized states."""

    return DEFAULT_US_EMAIL_SIGNATURE if is_us_opportunity_state(value) else None
