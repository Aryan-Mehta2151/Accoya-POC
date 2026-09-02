"""Plain-text signature policy for outreach emails."""

from __future__ import annotations

from agent.normalization import normalize_state_code


LEGACY_US_EMAIL_SIGNATURE = """Doug Gillikin
Specification Manager (Associate AIA)
Accsys

Accsys Sales Office
Accoya USA
Building 470,
200 S Wilcox Dr.
Kingsport, TN
37660-5147"""

DEFAULT_US_EMAIL_SIGNATURE = """ARTURO LUGO
NORTH AMERICA ARCHITECTURE SEGMENT MANAGER
Accsys Technologies, Building 470, 200 S Wilcox Dr, Kingsport, TN 37660-5147"""

DEFAULT_NL_EMAIL_SIGNATURE = """LAURA KEILY

HEAD OF MARKETING
Accsys Technologies PLC, 4th Floor, 3 Moorgate Place, London, EC2R 6EA"""

# Structured fields that back both the plain-text and HTML signatures.
DEFAULT_US_SIGNATURE_NAME = "ARTURO LUGO"
DEFAULT_US_SIGNATURE_TITLE = "NORTH AMERICA ARCHITECTURE SEGMENT MANAGER"
DEFAULT_US_SIGNATURE_COMPANY = "Accsys Technologies"
DEFAULT_US_SIGNATURE_ADDRESS_LINES = (
    "Building 470, 200 S Wilcox Dr, Kingsport, TN 37660-5147",
)

DEFAULT_NL_SIGNATURE_NAME = "LAURA KEILY"
DEFAULT_NL_SIGNATURE_TITLE = "HEAD OF MARKETING"
DEFAULT_NL_SIGNATURE_COMPANY = "Accsys Technologies PLC"
DEFAULT_NL_SIGNATURE_ADDRESS_LINES = (
    "4th Floor, 3 Moorgate Place, London, EC2R 6EA",
)

# Professional HTML signature rendered consistently across HTML mail clients.
DEFAULT_US_EMAIL_SIGNATURE_HTML = (
    "<div style=\"margin-top:24px;padding-top:14px;\">"
    "<div style=\"width:44px;height:2px;background:#5f6b66;margin:0 0 10px 0;\"></div>"
    "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" "
    "style=\"border-collapse:collapse;\">"
    "<tr><td style=\"border-left:4px solid #0f766e;padding:4px 0 4px 14px;"
    "font-family:'Segoe UI',Arial,sans-serif;\">"
    f"<div style=\"font-size:20px;font-weight:700;line-height:1.1;color:#0f172a;\">{DEFAULT_US_SIGNATURE_NAME}</div>"
    f"<div style=\"font-size:12px;letter-spacing:0.03em;text-transform:uppercase;color:#64748b;margin-top:4px;\">{DEFAULT_US_SIGNATURE_TITLE}</div>"
    f"<div style=\"font-size:14px;font-weight:700;color:#0f766e;margin-top:3px;\">{DEFAULT_US_SIGNATURE_COMPANY}</div>"
    "<div style=\"font-size:12px;color:#475569;line-height:1.52;margin-top:9px;\">"
    + "<br>".join(DEFAULT_US_SIGNATURE_ADDRESS_LINES)
    + "</div></td></tr></table></div>"
)

DEFAULT_NL_EMAIL_SIGNATURE_HTML = (
    "<div style=\"margin-top:24px;padding-top:14px;\">"
    "<div style=\"width:44px;height:2px;background:#5f6b66;margin:0 0 10px 0;\"></div>"
    "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" "
    "style=\"border-collapse:collapse;\">"
    "<tr><td style=\"border-left:4px solid #0f766e;padding:4px 0 4px 14px;"
    "font-family:'Segoe UI',Arial,sans-serif;\">"
    f"<div style=\"font-size:20px;font-weight:700;line-height:1.1;color:#0f172a;\">{DEFAULT_NL_SIGNATURE_NAME}</div>"
    f"<div style=\"font-size:12px;letter-spacing:0.03em;text-transform:uppercase;color:#64748b;margin-top:4px;\">{DEFAULT_NL_SIGNATURE_TITLE}</div>"
    f"<div style=\"font-size:14px;font-weight:700;color:#0f766e;margin-top:3px;\">{DEFAULT_NL_SIGNATURE_COMPANY}</div>"
    "<div style=\"font-size:12px;color:#475569;line-height:1.52;margin-top:9px;\">"
    + "<br>".join(DEFAULT_NL_SIGNATURE_ADDRESS_LINES)
    + "</div></td></tr></table></div>"
)

DEFAULT_EMAIL_SIGNATURES = (
    DEFAULT_US_EMAIL_SIGNATURE,
    DEFAULT_NL_EMAIL_SIGNATURE,
)


def signature_html_for(signature_text: str | None) -> str | None:
    """Return the structured HTML signature for a known plain-text signature."""

    if signature_text is None:
        return None
    if signature_text.strip() == DEFAULT_US_EMAIL_SIGNATURE.strip():
        return DEFAULT_US_EMAIL_SIGNATURE_HTML
    if signature_text.strip() == DEFAULT_NL_EMAIL_SIGNATURE.strip():
        return DEFAULT_NL_EMAIL_SIGNATURE_HTML
    return None

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
    """Return the market-specific signature for supported opportunities."""

    normalized_state = normalize_state_code(value)
    if normalized_state in US_STATE_CODES:
        return DEFAULT_US_EMAIL_SIGNATURE
    if normalized_state == "NL":
        return DEFAULT_NL_EMAIL_SIGNATURE
    return None


def effective_signature_for_state(
    signature_text: str | None,
    state_value: object,
) -> str | None:
    """Return a signature only for supported US and Netherlands opportunities."""

    default_signature = default_signature_for_state(state_value)
    if default_signature is None:
        return None
    if signature_text is not None and signature_text.strip() == LEGACY_US_EMAIL_SIGNATURE.strip():
        return DEFAULT_US_EMAIL_SIGNATURE
    if signature_text is not None and signature_text.strip():
        return signature_text
    return default_signature
