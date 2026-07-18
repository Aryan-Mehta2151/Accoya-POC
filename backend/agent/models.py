"""Typed contracts shared by the isolated Accoya email agent.

The models in this module deliberately contain no database or provider code.  They
are the stable boundary between normalization, deterministic routing, retrieval,
generation, validation, and callers of :class:`AccoyaEmailAgent`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


MIN_SELECTION_CONFIDENCE = 0.60


class AudienceType(str, Enum):
    """Supported lead-recipient categories."""

    ARCHITECT_SPECIFIER = "architect_specifier"
    CONTRACTOR_BUILDER = "contractor_builder"
    OWNER_DEVELOPER = "owner_developer"
    DISTRIBUTOR_SUPPLIER = "distributor_supplier"
    MANUFACTURER_FABRICATOR = "manufacturer_fabricator"
    FACILITIES_PROPERTY = "facilities_property"
    UNKNOWN = "unknown"


class ProjectStage(str, Enum):
    """Coarse project stage used for bounded CTA routing."""

    PLANNING = "planning"
    SPECIFICATION = "specification"
    PROCUREMENT = "procurement"
    UNKNOWN = "unknown"


class CTAType(str, Enum):
    """Low-friction calls to action allowed by the fixed policy."""

    TECHNICAL_INFORMATION = "technical_information"
    SAMPLE = "sample"
    SPECIFICATION_DISCUSSION = "specification_discussion"
    CSI_LANGUAGE = "csi_language"
    TECHNICAL_REVIEW = "technical_review"
    MANUFACTURER_COORDINATION = "manufacturer_coordination"
    SUPPLIER_ASSISTANCE = "supplier_assistance"
    AVAILABILITY_DISCUSSION = "availability_discussion"
    CLARIFY_NEEDS = "clarify_needs"


class SelectionStatus(str, Enum):
    """Whether product routing produced a usable catalog selection."""

    SELECTED = "selected"
    LOW_CONFIDENCE = "low_confidence"


class GenerationStatus(str, Enum):
    """Terminal outcomes exposed by the synchronous public API."""

    GENERATED = "generated"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    VALIDATION_FAILED = "validation_failed"
    PROVIDER_ERROR = "provider_error"


class ValidationStatus(str, Enum):
    """Deterministic validation state for a draft/result."""

    NOT_VALIDATED = "not_validated"
    VALID = "valid"
    INVALID = "invalid"


class EvidenceSource(str, Enum):
    """Closed set of permitted grounding sources."""

    LEAD = "lead"
    STRATEGY = "strategy"
    CATALOG = "catalog"
    POLICY = "policy"


class ViolationSeverity(str, Enum):
    """Severity of a deterministic validation finding."""

    ERROR = "error"
    WARNING = "warning"


class ValidationCode(str, Enum):
    """Stable machine-readable validation codes."""

    SUBJECT_TOO_LONG = "subject_too_long"
    BODY_WORD_COUNT = "body_word_count"
    PARAGRAPH_COUNT = "paragraph_count"
    PRODUCT_COUNT = "product_count"
    UNKNOWN_PRODUCT = "unknown_product"
    UNKNOWN_APPLICATION = "unknown_application"
    APPLICATION_FAMILY_MISMATCH = "application_family_mismatch"
    CTA_COUNT = "cta_count"
    CTA_STAGE_MISMATCH = "cta_stage_mismatch"
    PROHIBITED_PHRASE = "prohibited_phrase"
    COMPETITOR_UNGROUNDED = "competitor_ungrounded"
    WARRANTY_UNSUPPORTED = "warranty_unsupported"
    RECIPIENT_UNGROUNDED = "recipient_ungrounded"
    PROJECT_FACT_UNGROUNDED = "project_fact_ungrounded"
    OPENING_UNGROUNDED = "opening_ungrounded"
    TOO_MANY_BENEFITS = "too_many_benefits"
    STRATEGY_SOURCE_UNAPPROVED = "strategy_source_unapproved"
    EVIDENCE_INVALID = "evidence_invalid"
    SELECTION_MISMATCH = "selection_mismatch"
    PLANNING_FINALITY = "planning_finality"
    MANUFACTURED_SYSTEM_CLAIM = "manufactured_system_claim"


class AgentModel(BaseModel):
    """Strict base model for provider-facing and internal contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Contact(AgentModel):
    """Best-effort structured contact parsed from the source lead."""

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    title: str | None = None
    organization: str | None = None
    raw: str | None = None


class EvidenceReference(AgentModel):
    """An exact quote and identifier from an allowed grounding source."""

    source_type: EvidenceSource
    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    source_field: str | None = None
    normalized_value: str | None = None


class NormalizedLead(AgentModel):
    """Canonical, provider-safe representation of one complete lead record."""

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
    """Deterministic catalog-backed hint supplied before model selection."""

    product_family: str = Field(min_length=1)
    application: str = Field(min_length=1)
    source_field: str = Field(min_length=1)
    source_trigger: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    priority: int = Field(default=100, ge=0)


class ProductSelection(AgentModel):
    """Structured result of lead analysis and product selection."""

    audience: AudienceType = AudienceType.UNKNOWN
    project_name: str | None = None
    project_stage: ProjectStage = ProjectStage.UNKNOWN
    project_application: str | None = None
    material_signal: str | None = None
    named_competitor: str | None = None
    selected_product_family: str | None = None
    selected_application: str | None = None
    selection_reason: str = ""
    exact_source_trigger: EvidenceReference | None = None
    cta_type: CTAType = CTAType.CLARIFY_NEEDS
    benefit_topics: list[str] = Field(default_factory=list)
    retrieval_query: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    missing_information: list[str] = Field(default_factory=list)
    selection_status: SelectionStatus = SelectionStatus.SELECTED

    @model_validator(mode="after")
    def enforce_explicit_low_confidence(self) -> ProductSelection:
        """Keep nullable product fields exclusive to explicit low confidence."""

        family = self.selected_product_family
        application = self.selected_application
        if self.selection_status is SelectionStatus.LOW_CONFIDENCE:
            if family is not None or application is not None:
                raise ValueError(
                    "low_confidence selections must not contain product fields"
                )
            return self

        if not family or not application:
            raise ValueError("selected product family and application are required")
        if self.exact_source_trigger is None:
            raise ValueError("a selected product requires exact source evidence")
        return self


class StrategyChunk(AgentModel):
    """Approved Bedrock Knowledge Base retrieval result."""

    document_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None
    source_location: dict[str, Any] | str | None = None

    @property
    def is_approved(self) -> bool:
        """Return whether post-retrieval metadata explicitly approves this chunk."""

        return str(self.metadata.get("status", "")).strip().casefold() == "approved"


class BenefitClaim(AgentModel):
    """One draft benefit and the ledger entries that support it."""

    topic: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    evidence: list[EvidenceReference] = Field(default_factory=list)


class EmailDraftComponents(AgentModel):
    """Structured provider output assembled deterministically into an email."""

    subject: str
    opening_paragraph: str
    value_paragraph: str
    closing_paragraph: str | None = None
    selected_product_family: str
    selected_application: str
    lead_evidence_used: list[EvidenceReference] = Field(default_factory=list)
    strategy_source_ids: list[str] = Field(default_factory=list)
    benefits: list[BenefitClaim] = Field(default_factory=list)
    cta_type: CTAType
    cta_text: str
    competitor_mentions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EmailDraft(AgentModel):
    """Deterministically assembled draft prior to validation."""

    # Keep these fields required, but let deterministic validation handle blank
    # provider output so the bounded repair pass can correct it.
    subject: str
    body: str
    selected_product_family: str
    selected_application: str
    lead_evidence_used: list[EvidenceReference] = Field(default_factory=list)
    strategy_source_ids: list[str] = Field(default_factory=list)
    benefits: list[BenefitClaim] = Field(default_factory=list)
    cta_type: CTAType
    cta_text: str
    competitor_mentions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    validation_status: ValidationStatus = ValidationStatus.NOT_VALIDATED


def assemble_email_draft(components: EmailDraftComponents) -> EmailDraft:
    """Assemble two or three nonblank provider components into one body."""

    paragraphs = [
        paragraph.strip()
        for paragraph in (
            components.opening_paragraph,
            components.value_paragraph,
            components.closing_paragraph,
        )
        if paragraph and paragraph.strip()
    ]
    return EmailDraft(
        subject=components.subject,
        body="\n\n".join(paragraphs),
        selected_product_family=components.selected_product_family,
        selected_application=components.selected_application,
        lead_evidence_used=list(components.lead_evidence_used),
        strategy_source_ids=list(components.strategy_source_ids),
        benefits=list(components.benefits),
        cta_type=components.cta_type,
        cta_text=components.cta_text,
        competitor_mentions=list(components.competitor_mentions),
        warnings=list(components.warnings),
    )


class ValidationViolation(AgentModel):
    """One actionable deterministic validation failure."""

    code: ValidationCode | str
    message: str = Field(min_length=1)
    field: str | None = None
    offending_value: str | None = None
    severity: ViolationSeverity = ViolationSeverity.ERROR


class TokenUsage(AgentModel):
    """Provider token counts, when exposed by the model client."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class GenerationTelemetry(AgentModel):
    """Bounded execution metadata recorded without configuring global logging."""

    model_name: str = ""
    prompt_version: str = ""
    latency_ms: int | None = Field(default=None, ge=0)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    model_calls: int = Field(default=0, ge=0)
    retrieval_count: int = Field(default=0, ge=0)
    retrieval_query: str = ""
    retrieved_document_ids: list[str] = Field(default_factory=list)
    repair_attempted: bool = False
    node_timings_ms: dict[str, int] = Field(default_factory=dict)


class GenerationResult(AgentModel):
    """Terminal response returned by :meth:`AccoyaEmailAgent.generate`."""

    status: GenerationStatus
    lead_id: str = Field(min_length=1)
    original_lead: dict[str, Any]
    subject: str | None = None
    body: str | None = None
    selected_product_family: str | None = None
    selected_application: str | None = None
    evidence: list[EvidenceReference] = Field(default_factory=list)
    strategy_references: list[StrategyChunk] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    validation_status: ValidationStatus = ValidationStatus.NOT_VALIDATED
    validation_violations: list[ValidationViolation] = Field(default_factory=list)
    prompt_version: str
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    telemetry: GenerationTelemetry = Field(default_factory=GenerationTelemetry)

    @model_validator(mode="after")
    def hide_invalid_drafts(self) -> GenerationResult:
        """Expose subject/body only for a validated generated result."""

        if self.status is GenerationStatus.GENERATED:
            if not self.subject or not self.body:
                raise ValueError("generated results require a subject and body")
            if self.validation_status is not ValidationStatus.VALID:
                raise ValueError("generated results must have valid validation status")
        elif self.subject is not None or self.body is not None:
            raise ValueError("non-generated results must not expose a draft")
        return self


class AgentState(TypedDict, total=False):
    """Typed LangGraph state; nodes return bounded partial updates."""

    original_lead: dict[str, Any]
    normalized_lead: NormalizedLead
    routing_hints: list[RoutingHint]
    selection: ProductSelection
    strategy_chunks: list[StrategyChunk]
    draft_components: EmailDraftComponents
    draft: EmailDraft
    warnings: list[str]
    validation_violations: list[ValidationViolation]
    repair_attempted: bool
    error: str | None
    result: GenerationResult
    telemetry: GenerationTelemetry


StableIdKey = Literal["lead_id", "id", "external_id"]


__all__ = [
    "AgentState",
    "AudienceType",
    "BenefitClaim",
    "Contact",
    "CTAType",
    "EmailDraft",
    "EmailDraftComponents",
    "EvidenceReference",
    "EvidenceSource",
    "GenerationResult",
    "GenerationStatus",
    "GenerationTelemetry",
    "MIN_SELECTION_CONFIDENCE",
    "NormalizedLead",
    "ProductSelection",
    "ProjectStage",
    "RoutingHint",
    "SelectionStatus",
    "StableIdKey",
    "StrategyChunk",
    "TokenUsage",
    "ValidationCode",
    "ValidationStatus",
    "ValidationViolation",
    "ViolationSeverity",
    "assemble_email_draft",
]
