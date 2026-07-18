"""Deterministic three-node LangGraph workflow for one Accoya email draft."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from app.config import Settings, get_settings

from .catalog import application_belongs_to_family, get_application, get_family
from .integrations import (
    BedrockStrategyRetriever,
    GeminiStructuredModel,
    StrategyRetriever,
    StructuredModel,
    UnavailableStructuredModel,
)
from .models import (
    AgentState,
    CTAType,
    EmailDraft,
    EmailDraftComponents,
    EvidenceReference,
    GenerationResult,
    GenerationStatus,
    GenerationTelemetry,
    MIN_SELECTION_CONFIDENCE,
    NormalizedLead,
    ProductSelection,
    ProjectStage,
    RoutingHint,
    SelectionStatus,
    StrategyChunk,
    ValidationStatus,
    ValidationViolation,
    assemble_email_draft,
)
from .normalization import normalize_lead
from .observability import (
    add_usage,
    finish_telemetry,
    log_result,
    monotonic_ms,
    record_node_timing,
)
from .policy import PROMPT_VERSION, is_cta_allowed
from .prompts import (
    SYSTEM_PROMPT,
    build_analysis_prompt,
    build_compose_prompt,
    build_repair_prompt,
)
from .routing import get_routing_hints, routing_term_supported
from .validation import validate_email


Clock = Callable[[], datetime]


class _UnavailableStrategyRetriever:
    """Preserve a construction failure as a safe retrieval-time fallback."""

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        metadata_filters: Mapping[str, str] | None = None,
    ) -> list[StrategyChunk]:
        del query, top_k, metadata_filters
        raise RuntimeError("Bedrock Knowledge Base client is unavailable")


class AccoyaEmailAgent:
    """Synchronous, dependency-injectable wrapper around the compiled graph."""

    def __init__(
        self,
        *,
        model: StructuredModel,
        retriever: StrategyRetriever | None,
        top_k: int = 5,
        metadata_filters: Mapping[str, str] | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not 4 <= top_k <= 6:
            raise ValueError("top_k must be between 4 and 6")
        self._model = model
        self._retriever = retriever
        self._top_k = top_k
        self._metadata_filters = dict(metadata_filters or {})
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._graph = self._build_graph()

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "AccoyaEmailAgent":
        """Construct folder-local providers from the backend's existing settings."""

        configured = settings or get_settings()
        try:
            model: StructuredModel = GeminiStructuredModel.from_settings(configured)
        except ValueError:
            model = UnavailableStructuredModel(
                model_name=configured.gemini_model,
                reason="Gemini model configuration is incomplete.",
            )
        retriever: StrategyRetriever | None
        if configured.bedrock_kb_id.strip():
            try:
                retriever = BedrockStrategyRetriever.from_settings(configured)
            except Exception:
                # Client construction can fail for malformed regions, credentials,
                # or an unavailable local credential provider. Retrieval is an
                # optional enrichment, so preserve the safe catalog-only path.
                retriever = _UnavailableStrategyRetriever()
        else:
            retriever = None
        return cls(
            model=model,
            retriever=retriever,
            top_k=max(4, min(6, configured.bedrock_kb_top_k)),
        )

    @property
    def graph(self) -> Any:
        """Expose the compiled graph for inspection without persistence hooks."""

        return self._graph

    def generate(self, complete_lead_record: Mapping[str, Any]) -> GenerationResult:
        """Generate at most one validated draft from one complete lead mapping.

        Invalid request data, including a missing stable identifier, raises the
        normal adapter validation error. Runtime provider failures are represented
        by a safe result with no subject or body.
        """

        if not isinstance(complete_lead_record, Mapping):
            raise TypeError("complete_lead_record must be a mapping")
        original = deepcopy(dict(complete_lead_record))
        started_ms = monotonic_ms()
        initial: AgentState = {
            "original_lead": original,
            "strategy_chunks": [],
            "warnings": [],
            "validation_violations": [],
            "repair_attempted": False,
            "error": None,
            "telemetry": GenerationTelemetry(
                model_name=self._model.model_name,
                prompt_version=PROMPT_VERSION,
            ),
        }
        terminal = self._graph.invoke(initial)
        result = terminal.get("result")
        if result is None:
            raise RuntimeError("Accoya email graph completed without a result")
        telemetry = finish_telemetry(result.telemetry, run_started_ms=started_ms)
        result = result.model_copy(update={"telemetry": telemetry})
        log_result(result)
        return result

    def _build_graph(self) -> Any:
        builder = StateGraph(AgentState)
        builder.add_node("analyze_lead", self._analyze_lead)
        builder.add_node("retrieve_strategy", self._retrieve_strategy)
        builder.add_node("compose_and_validate", self._compose_and_validate)
        builder.add_edge(START, "analyze_lead")
        builder.add_edge("analyze_lead", "retrieve_strategy")
        builder.add_edge("retrieve_strategy", "compose_and_validate")
        builder.add_edge("compose_and_validate", END)
        return builder.compile()

    def _analyze_lead(self, state: AgentState) -> dict[str, Any]:
        started_ms = monotonic_ms()
        telemetry = state["telemetry"]
        lead = normalize_lead(state["original_lead"])
        hints = get_routing_hints(lead)
        try:
            invocation = self._model.invoke_structured(
                ProductSelection,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=build_analysis_prompt(lead, hints),
                temperature=0.1,
            )
            telemetry = add_usage(telemetry, invocation.usage)
            selection = _normalize_selection(invocation.value, lead, hints)
            warnings = list(state.get("warnings", []))
            if selection.selection_status is SelectionStatus.LOW_CONFIDENCE:
                warnings.append(
                    "Lead evidence does not support a confident catalog product selection."
                )
            error = None
        except Exception:
            telemetry = _record_failed_model_call(telemetry)
            selection = _low_confidence_selection(
                lead,
                reason="Lead analysis provider was unavailable.",
                missing=["structured lead analysis"],
            )
            warnings = [
                *state.get("warnings", []),
                "Lead analysis failed; no draft was generated.",
            ]
            error = "analysis_provider_error"

        telemetry = record_node_timing(telemetry, "analyze_lead", started_ms)
        return {
            "normalized_lead": lead,
            "routing_hints": hints,
            "selection": selection,
            "warnings": warnings,
            "error": error,
            "telemetry": telemetry,
        }

    def _retrieve_strategy(self, state: AgentState) -> dict[str, Any]:
        started_ms = monotonic_ms()
        telemetry = state["telemetry"]
        warnings = list(state.get("warnings", []))
        selection = state["selection"]
        chunks: list[StrategyChunk] = []

        if state.get("error") is None and selection.selection_status is SelectionStatus.SELECTED:
            query = selection.retrieval_query.strip()
            telemetry = telemetry.model_copy(update={"retrieval_query": query})
            if self._retriever is None:
                warnings.append(
                    "Knowledge Base is not configured; using catalog-only positioning."
                )
            else:
                try:
                    retrieved = self._retriever.retrieve(
                        query,
                        top_k=self._top_k,
                        metadata_filters=self._metadata_filters,
                    )
                    chunks = [chunk for chunk in retrieved if chunk.is_approved]
                    if not chunks:
                        warnings.append(
                            "No approved strategy documents were retrieved; using catalog-only positioning."
                        )
                except Exception:
                    warnings.append(
                        "Approved strategy retrieval was unavailable; using catalog-only positioning."
                    )

        telemetry = telemetry.model_copy(
            update={
                "retrieval_count": len(chunks),
                "retrieved_document_ids": [chunk.document_id for chunk in chunks],
            }
        )
        telemetry = record_node_timing(telemetry, "retrieve_strategy", started_ms)
        return {
            "strategy_chunks": chunks,
            "warnings": warnings,
            "telemetry": telemetry,
        }

    def _compose_and_validate(self, state: AgentState) -> dict[str, Any]:
        started_ms = monotonic_ms()
        lead = state["normalized_lead"]
        selection = state["selection"]
        chunks = state.get("strategy_chunks", [])
        warnings = list(state.get("warnings", []))
        telemetry = state["telemetry"]

        if state.get("error"):
            result = self._result(
                state,
                status=GenerationStatus.PROVIDER_ERROR,
                validation_status=ValidationStatus.NOT_VALIDATED,
                warnings=warnings,
                telemetry=telemetry,
            )
            return self._finish_compose_state(
                state, result, telemetry, started_ms, warnings=warnings
            )

        if selection.selection_status is SelectionStatus.LOW_CONFIDENCE:
            result = self._result(
                state,
                status=GenerationStatus.INSUFFICIENT_CONTEXT,
                validation_status=ValidationStatus.NOT_VALIDATED,
                warnings=warnings,
                telemetry=telemetry,
            )
            return self._finish_compose_state(
                state, result, telemetry, started_ms, warnings=warnings
            )

        try:
            invocation = self._model.invoke_structured(
                EmailDraftComponents,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=build_compose_prompt(lead, selection, chunks),
                temperature=0.3,
            )
            telemetry = add_usage(telemetry, invocation.usage)
            components = invocation.value
            draft = assemble_email_draft(components)
        except Exception:
            telemetry = _record_failed_model_call(telemetry)
            warnings.append("Email composition provider failed; no draft was returned.")
            result = self._result(
                state,
                status=GenerationStatus.PROVIDER_ERROR,
                validation_status=ValidationStatus.NOT_VALIDATED,
                warnings=warnings,
                telemetry=telemetry,
            )
            return self._finish_compose_state(
                state, result, telemetry, started_ms, warnings=warnings
            )

        violations = validate_email(
            draft,
            lead=lead,
            selection=selection,
            strategy_chunks=chunks,
        )
        repair_attempted = False
        if violations:
            repair_attempted = True
            telemetry = telemetry.model_copy(update={"repair_attempted": True})
            try:
                repair = self._model.invoke_structured(
                    EmailDraftComponents,
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=build_repair_prompt(
                        lead=lead,
                        selection=selection,
                        chunks=chunks,
                        draft=components,
                        violations=violations,
                    ),
                    temperature=0.1,
                )
                telemetry = add_usage(telemetry, repair.usage)
                components = repair.value
                draft = assemble_email_draft(components)
                violations = validate_email(
                    draft,
                    lead=lead,
                    selection=selection,
                    strategy_chunks=chunks,
                )
            except Exception:
                telemetry = _record_failed_model_call(telemetry)
                warnings.append("The single repair pass failed; the invalid draft was withheld.")

        if violations:
            result = self._result(
                state,
                status=GenerationStatus.VALIDATION_FAILED,
                validation_status=ValidationStatus.INVALID,
                warnings=warnings,
                violations=violations,
                telemetry=telemetry,
            )
        else:
            draft = draft.model_copy(update={"validation_status": ValidationStatus.VALID})
            used_ids = set(draft.strategy_source_ids)
            used_chunks = [chunk for chunk in chunks if chunk.document_id in used_ids]
            result = self._result(
                state,
                status=GenerationStatus.GENERATED,
                validation_status=ValidationStatus.VALID,
                warnings=[*warnings, *draft.warnings],
                telemetry=telemetry,
                draft=draft,
                strategy_references=used_chunks,
            )

        return self._finish_compose_state(
            state,
            result,
            telemetry,
            started_ms,
            draft=draft,
            components=components,
            warnings=result.warnings,
            violations=violations,
            repair_attempted=repair_attempted,
        )

    def _finish_compose_state(
        self,
        state: AgentState,
        result: GenerationResult,
        telemetry: GenerationTelemetry,
        started_ms: int,
        *,
        draft: EmailDraft | None = None,
        components: EmailDraftComponents | None = None,
        warnings: list[str],
        violations: list[ValidationViolation] | None = None,
        repair_attempted: bool = False,
    ) -> dict[str, Any]:
        telemetry = record_node_timing(
            telemetry, "compose_and_validate", started_ms
        )
        result = result.model_copy(update={"telemetry": telemetry})
        update: dict[str, Any] = {
            "result": result,
            "telemetry": telemetry,
            "warnings": warnings,
            "validation_violations": list(violations or []),
            "repair_attempted": repair_attempted,
        }
        if draft is not None:
            update["draft"] = draft
        if components is not None:
            update["draft_components"] = components
        return update

    def _result(
        self,
        state: AgentState,
        *,
        status: GenerationStatus,
        validation_status: ValidationStatus,
        warnings: list[str],
        telemetry: GenerationTelemetry,
        violations: list[ValidationViolation] | None = None,
        draft: EmailDraft | None = None,
        strategy_references: list[StrategyChunk] | None = None,
    ) -> GenerationResult:
        selection = state["selection"]
        return GenerationResult(
            status=status,
            lead_id=state["normalized_lead"].lead_id,
            original_lead=deepcopy(state["original_lead"]),
            subject=draft.subject if draft else None,
            body=draft.body if draft else None,
            selected_product_family=(
                draft.selected_product_family
                if draft
                else selection.selected_product_family
            ),
            selected_application=(
                draft.selected_application if draft else selection.selected_application
            ),
            evidence=list(draft.lead_evidence_used) if draft else [],
            strategy_references=list(strategy_references or []),
            warnings=_deduplicate_strings(warnings),
            validation_status=validation_status,
            validation_violations=list(violations or []),
            prompt_version=PROMPT_VERSION,
            generated_at=_utc(self._clock()),
            telemetry=telemetry,
        )


def _normalize_selection(
    selection: ProductSelection,
    lead: NormalizedLead,
    hints: list[RoutingHint],
) -> ProductSelection:
    """Canonicalize and deterministically accept or reject a model selection."""

    if selection.selection_status is SelectionStatus.LOW_CONFIDENCE:
        return _low_confidence_selection(
            lead,
            reason=selection.selection_reason or "Model reported low confidence.",
            missing=selection.missing_information,
            confidence=min(selection.confidence, MIN_SELECTION_CONFIDENCE - 0.01),
        )

    family = get_family(selection.selected_product_family)
    application = get_application(
        selection.selected_application,
        family_id=family.id if family else None,
    )
    evidence = selection.exact_source_trigger
    pair_supported = bool(
        family
        and application
        and application_belongs_to_family(family.id, application.id)
        and _catalog_pair_supported(family.id, application.id, lead, hints)
    )
    if (
        not pair_supported
        or evidence is None
        or not _lead_evidence_supported(evidence, lead)
        or not _selection_trigger_supported(
            evidence,
            family.id if family else "",
            application.id if application else "",
            hints,
        )
        or selection.confidence < MIN_SELECTION_CONFIDENCE
    ):
        return _low_confidence_selection(
            lead,
            reason="The proposed product selection was not supported by exact lead/catalog evidence.",
            missing=[*selection.missing_information, "supported product/application evidence"],
            confidence=min(selection.confidence, MIN_SELECTION_CONFIDENCE - 0.01),
        )

    named_competitor = _matched_normalized_signal(
        selection.named_competitor, lead.competitor_mentions
    )
    material_signal = _matched_normalized_signal(
        selection.material_signal, lead.material_mentions
    )
    if material_signal is None and named_competitor is not None:
        material_signal = named_competitor

    cta_type = selection.cta_type
    if not is_cta_allowed(lead.project_stage, cta_type):
        cta_type = _default_cta(lead.project_stage)
    benefit_topics = selection.benefit_topics[:3]
    canonical = selection.model_dump()
    canonical.update(
        {
            "audience": lead.audience,
            "project_name": lead.project,
            "project_stage": lead.project_stage,
            "project_application": application.display_name,
            "material_signal": material_signal,
            "named_competitor": named_competitor,
            "selected_product_family": family.id,
            "selected_application": application.id,
            "cta_type": cta_type,
            "benefit_topics": benefit_topics,
            "retrieval_query": _retrieval_query(
                lead,
                family.display_name,
                application.display_name,
                material_signal,
                named_competitor,
                benefit_topics,
                cta_type,
            ),
            "selection_status": SelectionStatus.SELECTED,
        }
    )
    return ProductSelection.model_validate(canonical)


def _low_confidence_selection(
    lead: NormalizedLead,
    *,
    reason: str,
    missing: list[str],
    confidence: float = 0.0,
) -> ProductSelection:
    return ProductSelection(
        audience=lead.audience,
        project_name=lead.project,
        project_stage=lead.project_stage,
        project_application=None,
        material_signal=None,
        named_competitor=None,
        selected_product_family=None,
        selected_application=None,
        selection_reason=reason,
        exact_source_trigger=None,
        cta_type=_default_cta(lead.project_stage),
        benefit_topics=[],
        retrieval_query="",
        confidence=max(0.0, min(confidence, MIN_SELECTION_CONFIDENCE - 0.01)),
        missing_information=_deduplicate_strings(missing),
        selection_status=SelectionStatus.LOW_CONFIDENCE,
    )


def _catalog_pair_supported(
    family_id: str,
    application_id: str,
    lead: NormalizedLead,
    hints: list[RoutingHint],
) -> bool:
    if any(
        hint.product_family == family_id and hint.application == application_id
        for hint in hints
    ):
        return True
    application = get_application(application_id, family_id=family_id)
    if application is None:
        return False
    lead_text = _lead_text(lead)
    terms = (
        application.display_name,
        *application.aliases,
        *application.routing_terms,
    )
    return any(
        routing_term_supported(term, lead_text)
        for term in terms
        if term.strip()
    )


def _lead_evidence_supported(
    evidence: EvidenceReference, lead: NormalizedLead
) -> bool:
    if (
        str(getattr(evidence.source_type, "value", evidence.source_type)) != "lead"
        or evidence.source_id != lead.lead_id
        or not evidence.source_field
    ):
        return False
    value = getattr(lead, evidence.source_field, None)
    if value is None:
        normalized_key = _key_token(evidence.source_field)
        value = next(
            (
                item
                for key, item in lead.source_values.items()
                if _key_token(key) == normalized_key
            ),
            None,
        )
    return _specific_evidence_quote(evidence.quote) and _contains_phrase(
        _stringify(value), evidence.quote
    )


def _selection_trigger_supported(
    evidence: EvidenceReference,
    family_id: str,
    application_id: str,
    hints: list[RoutingHint],
) -> bool:
    """Require the exact quote to carry the signal for the selected pair."""

    for hint in hints:
        if (
            hint.product_family == family_id
            and hint.application == application_id
            and _key_token(hint.source_field) == _key_token(evidence.source_field)
            and _contains_phrase(evidence.quote, hint.source_trigger)
        ):
            return True

    family = get_family(family_id)
    application = get_application(application_id, family_id=family_id)
    if family is None or application is None:
        return False
    terms = (
        family.display_name,
        *(term for term in family.aliases if term.casefold() != "accoya"),
        application.display_name,
        *application.aliases,
        *application.routing_terms,
    )
    return any(
        routing_term_supported(term, evidence.quote)
        for term in terms
        if term.strip()
    )


def _matched_normalized_signal(
    candidate: str | None,
    references: list[EvidenceReference],
) -> str | None:
    if not candidate or not candidate.strip():
        return None
    return (
        candidate.strip()
        if any(_contains_phrase(reference.quote, candidate) for reference in references)
        else None
    )


def _specific_evidence_quote(value: str) -> bool:
    words = [word for word in re.findall(r"[a-z0-9]+", value.casefold()) if word]
    if not words:
        return False
    if len(words) > 1:
        return len(value.strip()) >= 5
    return len(words[0]) >= 4 and words[0] not in {
        "material",
        "planning",
        "project",
        "review",
    }


def _contains_phrase(text: str, phrase: str) -> bool:
    escaped = re.escape(phrase.strip())
    return bool(
        escaped
        and re.search(rf"(?<!\w){escaped}(?!\w)", text, re.IGNORECASE)
    )


def _retrieval_query(
    lead: NormalizedLead,
    family: str,
    application: str,
    material: str | None,
    competitor: str | None,
    benefit_topics: list[str],
    cta_type: CTAType,
) -> str:
    parts = [
        "approved Accoya strategy positioning",
        family,
        application,
        f"audience {lead.audience.value}",
        f"project stage {lead.project_stage.value}",
        f"CTA {cta_type.value}",
    ]
    if material:
        parts.append(f"material signal {material}")
    if competitor:
        parts.append(f"named competitor {competitor}")
    if lead.priority_reasons:
        parts.append(f"project need {lead.priority_reasons}")
    elif lead.summary:
        parts.append(f"project need {lead.summary}")
    if benefit_topics:
        parts.append("topics " + ", ".join(benefit_topics[:3]))
    return "; ".join(parts)


def _default_cta(stage: ProjectStage) -> CTAType:
    return {
        ProjectStage.PLANNING: CTAType.SPECIFICATION_DISCUSSION,
        ProjectStage.SPECIFICATION: CTAType.TECHNICAL_REVIEW,
        ProjectStage.PROCUREMENT: CTAType.AVAILABILITY_DISCUSSION,
        ProjectStage.UNKNOWN: CTAType.CLARIFY_NEEDS,
    }[stage]


def _lead_text(lead: NormalizedLead) -> str:
    return _stringify(lead.source_values)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return " ".join(f"{key} {_stringify(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_stringify(item) for item in value)
    return str(value)


def _key_token(value: Any) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _record_failed_model_call(telemetry: GenerationTelemetry) -> GenerationTelemetry:
    return telemetry.model_copy(update={"model_calls": telemetry.model_calls + 1})


def _deduplicate_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value).strip()
        if clean and clean.casefold() not in seen:
            seen.add(clean.casefold())
            result.append(clean)
    return result


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = ["AccoyaEmailAgent"]
