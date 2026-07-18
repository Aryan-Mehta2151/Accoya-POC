"""Small typed contracts for the isolated Accoya email workflow."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


MIN_SELECTION_CONFIDENCE = 0.60


class AudienceType(str, Enum):
    ARCHITECT_SPECIFIER = "architect_specifier"
    CONTRACTOR_BUILDER = "contractor_builder"
    OWNER_DEVELOPER = "owner_developer"
    DISTRIBUTOR_SUPPLIER = "distributor_supplier"
    MANUFACTURER_FABRICATOR = "manufacturer_fabricator"
    FACILITIES_PROPERTY = "facilities_property"
    UNKNOWN = "unknown"


class ProjectStage(str, Enum):
    PLANNING = "planning"
    SPECIFICATION = "specification"
    PROCUREMENT = "procurement"
    UNKNOWN = "unknown"


class SelectionStatus(str, Enum):
    SELECTED = "selected"
    LOW_CONFIDENCE = "low_confidence"


class GenerationStatus(str, Enum):
    GENERATED = "generated"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    PROVIDER_ERROR = "provider_error"


class EvidenceSource(str, Enum):
    """Source marker retained for normalized material/competitor mentions."""

    LEAD = "lead"


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Contact(AgentModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    title: str | None = None
    organization: str | None = None
    raw: str | None = None


class EvidenceReference(AgentModel):
    """Exact normalized lead mention used only for analysis context."""

    source_type: EvidenceSource = EvidenceSource.LEAD
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    source_field: str | None = None
    normalized_value: str | None = None


class NormalizedLead(AgentModel):
    lead_id: str = Field(min_length=1)
    section: str | None = None
    project: str | None = None
    location: str | None = None
    state: str | None = None
    signal: str | None = None
    intelligence: str | None = None
    score: float | None = None
    timing: str | None = None
    next_step: str | None = None
    awarded_to: str | None = None
    priority_reasons: str | None = None
    summary: str | None = None
    contacts_raw: str | None = None
    meeting_date_raw: str | None = None
    tags_raw: str | None = None
    url: str | None = None
    contacts: list[Contact] = Field(default_factory=list)
    meeting_date: date | None = None
    tags: list[str] = Field(default_factory=list)
    city: str | None = None
    audience: AudienceType = AudienceType.UNKNOWN
    project_stage: ProjectStage = ProjectStage.UNKNOWN
    material_mentions: list[EvidenceReference] = Field(default_factory=list)
    competitor_mentions: list[EvidenceReference] = Field(default_factory=list)
    source_values: dict[str, Any] = Field(default_factory=dict)


class RoutingHint(AgentModel):
    product_family: str = Field(min_length=1)
    application: str = Field(min_length=1)
    source_field: str = Field(min_length=1)
    source_trigger: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    priority: int = Field(default=100, ge=0)


class ProductSelection(AgentModel):
    """One Gemini-selected canonical catalog pair or explicit no-selection."""

    selected_product_family: str | None = None
    selected_application: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    selection_reason: str = ""
    retrieval_query: str = ""
    selection_status: SelectionStatus = SelectionStatus.SELECTED

    @model_validator(mode="after")
    def enforce_selection_shape(self) -> ProductSelection:
        if self.selection_status is SelectionStatus.LOW_CONFIDENCE:
            if self.selected_product_family is not None or self.selected_application is not None:
                raise ValueError("low-confidence selection cannot contain a catalog pair")
            return self
        if not self.selected_product_family or not self.selected_application:
            raise ValueError("selected results require one product/application pair")
        if not self.retrieval_query:
            raise ValueError("selected results require a retrieval query")
        return self


class StrategyChunk(AgentModel):
    """Raw Bedrock Knowledge Base retrieval result supplied to Gemini."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    document_id: str = Field(min_length=1)
    text: str = ""
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: Any = None
    source_location: dict[str, Any] | str | None = None


class EmailDraft(AgentModel):
    """Minimal structured composition response."""

    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    selected_product_family: str = Field(min_length=1)
    selected_application: str = Field(min_length=1)


class TokenUsage(AgentModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class GenerationTelemetry(AgentModel):
    model_name: str = ""
    prompt_version: str = ""
    latency_ms: int | None = Field(default=None, ge=0)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    model_calls: int = Field(default=0, ge=0)
    retrieval_count: int = Field(default=0, ge=0)
    retrieval_query: str = ""
    retrieved_document_ids: list[str] = Field(default_factory=list)
    node_timings_ms: dict[str, int] = Field(default_factory=dict)


class GenerationResult(AgentModel):
    status: GenerationStatus
    lead_id: str = Field(min_length=1)
    original_lead: dict[str, Any]
    subject: str | None = None
    body: str | None = None
    selected_product_family: str | None = None
    selected_application: str | None = None
    strategy_references: list[StrategyChunk] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    prompt_version: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    telemetry: GenerationTelemetry = Field(default_factory=GenerationTelemetry)

    @model_validator(mode="after")
    def enforce_safe_result(self) -> GenerationResult:
        if self.status is GenerationStatus.GENERATED:
            if not self.subject or not self.body:
                raise ValueError("generated results require subject and body")
        elif self.subject is not None or self.body is not None:
            raise ValueError("non-generated results cannot expose a draft")
        return self


class AgentState(TypedDict, total=False):
    original_lead: dict[str, Any]
    normalized_lead: NormalizedLead
    routing_hints: list[RoutingHint]
    selection: ProductSelection
    strategy_chunks: list[StrategyChunk]
    draft: EmailDraft
    warnings: list[str]
    error: str | None
    result: GenerationResult
    telemetry: GenerationTelemetry


StableIdKey = Literal["lead_id", "id", "external_id"]


__all__ = [
    "AgentState", "AudienceType", "Contact", "EmailDraft", "EvidenceReference",
    "EvidenceSource", "GenerationResult", "GenerationStatus", "GenerationTelemetry",
    "MIN_SELECTION_CONFIDENCE", "NormalizedLead", "ProductSelection", "ProjectStage",
    "RoutingHint", "SelectionStatus", "StableIdKey", "StrategyChunk", "TokenUsage",
]
