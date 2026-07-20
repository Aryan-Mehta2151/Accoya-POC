"""Generate a personalized outreach email for a lead using RAG + Gemini."""
from app.db.models import Lead
from app.services import bedrock_service, gemini_service

_EMAIL_SYSTEM = (
    "You are an expert B2B copywriter writing a personalized cold outreach email "
    "on behalf of our client. Use the lead's details and the strategy context to "
    "craft a concise, relevant, non-spammy email. Return a subject line and a body."
)


def generate_email(lead: Lead) -> tuple[str, str]:
    """Return (subject, body) for the given lead/opportunity."""
    lead_summary = (
        f"Project: {lead.project}\n"
        f"Location: {lead.location}, {lead.state}\n"
        f"Section: {lead.section}\n"
        f"Signal: {lead.signal} | Stage: {lead.intelligence}\n"
        f"Timing: {lead.timing}\n"
        f"Why relevant: {lead.priority_reasons}\n"
        f"Summary: {lead.summary}\n"
        f"Contact: {lead.contacts}\n"
        f"Source: {lead.url}"
    )

    # Retrieve strategy context relevant to this opportunity.
    query = f"Outreach strategy for {lead.project or ''} in {lead.location or ''} {lead.state or ''}".strip()
    chunks = bedrock_service.retrieve(query)
    strategy_context = "\n\n---\n\n".join(c.text for c in chunks)

    prompt = (
        f"{_EMAIL_SYSTEM}\n\n"
        f"Strategy context:\n{strategy_context}\n\n"
        f"Lead:\n{lead_summary}\n\n"
        "Respond in exactly this format:\n"
        "SUBJECT: <subject line>\n"
        "BODY:\n<email body>"
    )

    raw = gemini_service.generate(prompt, temperature=0.6)
    return _parse(raw)


def _parse(raw: str) -> tuple[str, str]:
    subject, body = "", raw.strip()
    if "SUBJECT:" in raw:
        after = raw.split("SUBJECT:", 1)[1]
        if "BODY:" in after:
            subject_part, body_part = after.split("BODY:", 1)
            subject = subject_part.strip()
            body = body_part.strip()
        else:
            subject = after.strip()
    return subject, body
