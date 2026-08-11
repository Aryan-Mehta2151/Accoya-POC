"""Application service for generating lead emails with the Accoya agent."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Protocol

from agent import AccoyaEmailAgent
from agent.models import GenerationResult

from app.db.models import Lead


class EmailAgent(Protocol):
    """Minimal agent interface used by the email application service."""

    def generate(self, complete_lead_record: Mapping[str, Any]) -> GenerationResult:
        """Generate one email result from a curated lead mapping."""


# Keep this list explicit: ``NormalizedLead.source_values`` is included in model
# prompts, so passing an ORM ``__dict__`` or the complete raw feed row would expose
# fields that the email workflow has not deliberately approved.
_AGENT_LEAD_FIELDS = (
    "section",
    "project",
    "location",
    "state",
    "signal",
    "intelligence",
    "score",
    "timing",
    "next_step",
    "awarded_to",
    "priority_reasons",
    "summary",
    "contacts",
    "contact_email",
    "meeting_date",
    "tags",
    "url",
)


@lru_cache
def get_accoya_email_agent() -> AccoyaEmailAgent:
    """Return the process-wide agent instance used by FastAPI dependencies."""

    return AccoyaEmailAgent.from_settings()


def build_agent_lead(lead: Lead) -> dict[str, Any]:
    """Map a stored lead to the only fields approved for agent prompts."""

    payload: dict[str, Any] = {
        "lead_id": str(lead.id),
        "source_system": lead.source_system,
        "external_id": lead.external_id,
    }
    payload.update({field: getattr(lead, field) for field in _AGENT_LEAD_FIELDS})
    if isinstance(lead.raw_data, Mapping):
        source_state = lead.raw_data.get("state")
        if source_state is None:
            source_state = lead.raw_data.get("State")
        if source_state is not None:
            payload["source_state"] = source_state
    return payload


def generate_email(lead: Lead, agent: EmailAgent) -> GenerationResult:
    """Invoke the Accoya agent for one persisted lead."""

    return agent.generate(build_agent_lead(lead))
