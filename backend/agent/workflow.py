"""Three-node LangGraph workflow for one Accoya nurturing email."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
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
    EmailDraft,
    GenerationResult,
    GenerationStatus,
    GenerationTelemetry,
    MIN_SELECTION_CONFIDENCE,
    NormalizedLead,
    NurturingRoute,
    ProductSelection,
    RoutingHint,
    SelectionStatus,
    StrategyChunk,
)
from .normalization import normalize_lead
from .observability import (
    add_usage,
    finish_telemetry,
    log_result,
    monotonic_ms,
    record_node_timing,
)
from .prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_analysis_prompt,
    build_compose_prompt,
    build_nurturing_route_prompt,
)
from .routing import get_routing_hints


Clock = Callable[[], datetime]


class _UnavailableStrategyRetriever:
    def retrieve(self, query: str, *, top_k: int = 5) -> list[StrategyChunk]:
        del query, top_k
        raise RuntimeError("Knowledge Base client construction failed")


class AccoyaEmailAgent:
    """Public synchronous interface for the isolated email workflow."""

    def __init__(
        self,
        *,
        model: StructuredModel,
        retriever: StrategyRetriever | None = None,
        top_k: int = 5,
        clock: Clock | None = None,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        self._model = model
        self._retriever = retriever
        self._top_k = top_k
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._graph = self._build_graph()

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "AccoyaEmailAgent":
        configured = settings or get_settings()
        try:
            model: StructuredModel = GeminiStructuredModel.from_settings(configured)
        except ValueError:
            model = UnavailableStructuredModel(
                model_name=configured.gemini_model,
                reason="Gemini model configuration is incomplete.",
            )
        if configured.bedrock_kb_id.strip():
            try:
                retriever: StrategyRetriever | None = (
                    BedrockStrategyRetriever.from_settings(configured)
                )
            except Exception:
                retriever = _UnavailableStrategyRetriever()
        else:
            retriever = None
        return cls(
            model=model,
            retriever=retriever,
            top_k=max(1, configured.bedrock_kb_top_k),
        )

    @property
    def graph(self) -> Any:
        return self._graph

    def generate(self, complete_lead_record: Mapping[str, Any]) -> GenerationResult:
        if not isinstance(complete_lead_record, Mapping):
            raise TypeError("complete_lead_record must be a mapping")
        started_ms = monotonic_ms()
        initial: AgentState = {
            "original_lead": deepcopy(dict(complete_lead_record)),
            "strategy_chunks": [],
            "nurturing_chunks": [],
            "warnings": [],
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
        builder.add_node("compose_email", self._compose_email)
        builder.add_edge(START, "analyze_lead")
        builder.add_edge("analyze_lead", "retrieve_strategy")
        builder.add_edge("retrieve_strategy", "compose_email")
        builder.add_edge("compose_email", END)
        return builder.compile()

    def _analyze_lead(self, state: AgentState) -> dict[str, Any]:
        started_ms = monotonic_ms()
        telemetry = state["telemetry"]
        lead = normalize_lead(state["original_lead"])
        hints = get_routing_hints(lead)
        warnings = list(state.get("warnings", []))
        try:
            invocation = self._model.invoke_structured(
                ProductSelection,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=build_analysis_prompt(lead, hints),
                temperature=0.1,
            )
            telemetry = add_usage(telemetry, invocation.usage)
            selection = _normalize_selection(invocation.value)
            error = None
            if selection.selection_status is SelectionStatus.LOW_CONFIDENCE:
                warnings.append(
                    "Lead does not support a confident catalog product selection."
                )
        except Exception:
            telemetry = _record_failed_model_call(telemetry)
            selection = _fallback_selection_from_hints(lead, hints)
            if selection.selection_status is SelectionStatus.SELECTED:
                warnings.append(
                    "Lead analysis provider was unavailable; using deterministic routing fallback."
                )
                error = None
            else:
                warnings.append(
                    "Lead analysis provider was unavailable and no deterministic routing hint was available."
                )
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
        nurturing_chunks: list[StrategyChunk] = []

        if (
            state.get("error") is not None
            or selection.selection_status is SelectionStatus.LOW_CONFIDENCE
        ):
            telemetry = record_node_timing(
                telemetry, "retrieve_strategy", started_ms
            )
            return {
                "strategy_chunks": chunks,
                "nurturing_chunks": nurturing_chunks,
                "warnings": warnings,
                "telemetry": telemetry,
            }

        nurturing_route = _fallback_nurturing_route(state["normalized_lead"])
        query = selection.retrieval_query
        telemetry = telemetry.model_copy(update={"retrieval_query": query})
        if self._retriever is None:
            warnings.append(
                "Knowledge Base is not configured; generating without KB context."
            )
        else:
            try:
                chunks = self._retriever.retrieve(query, top_k=self._top_k)
                if not chunks:
                    warnings.append(
                        "Knowledge Base returned no results; generating without KB context."
                    )
            except Exception:
                warnings.append(
                    "Knowledge Base retrieval failed; generating without KB context."
                )

        try:
            route_invocation = self._model.invoke_structured(
                NurturingRoute,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=build_nurturing_route_prompt(
                    state["normalized_lead"],
                    selection,
                ),
                temperature=0.1,
            )
            telemetry = add_usage(telemetry, route_invocation.usage)
            nurturing_route = route_invocation.value
        except Exception:
            telemetry = _record_failed_model_call(telemetry)
            warnings.append(
                "Nurturing route selection failed; using deterministic fallback route."
            )

        nurturing_route = nurturing_route.model_copy(
            update={
                "retrieval_query": _nurturing_template_query(
                    nurturing_route.email_number
                )
            }
        )

        if self._retriever is None:
            warnings.append(
                "Nurturing Knowledge Base is not configured; generating without nurturing KB context."
            )
        else:
            try:
                nurturing_chunks = self._retriever.retrieve(
                    nurturing_route.retrieval_query,
                    top_k=self._top_k,
                )
                if not nurturing_chunks:
                    warnings.append(
                        "Nurturing template retrieval returned no results for selected email template."
                    )
            except Exception:
                warnings.append(
                    "Nurturing template retrieval failed; generating without nurturing KB context."
                )

        all_chunks = [*chunks, *nurturing_chunks]
        telemetry = telemetry.model_copy(
            update={
                "retrieval_count": len(all_chunks),
                "retrieved_document_ids": [
                    chunk.document_id for chunk in all_chunks
                ],
            }
        )
        telemetry = record_node_timing(telemetry, "retrieve_strategy", started_ms)
        return {
            "strategy_chunks": chunks,
            "nurturing_route": nurturing_route,
            "nurturing_chunks": nurturing_chunks,
            "warnings": warnings,
            "telemetry": telemetry,
        }

    def _compose_email(self, state: AgentState) -> dict[str, Any]:
        started_ms = monotonic_ms()
        telemetry = state["telemetry"]
        warnings = list(state.get("warnings", []))
        selection = state["selection"]
        chunks = state.get("strategy_chunks", [])

        if state.get("error"):
            result = self._result(
                state,
                status=GenerationStatus.PROVIDER_ERROR,
                telemetry=telemetry,
                warnings=warnings,
                strategy_references=chunks,
            )
            return self._finish_compose(result, telemetry, started_ms, warnings)

        if selection.selection_status is SelectionStatus.LOW_CONFIDENCE:
            result = self._result(
                state,
                status=GenerationStatus.INSUFFICIENT_CONTEXT,
                telemetry=telemetry,
                warnings=warnings,
                strategy_references=chunks,
            )
            return self._finish_compose(result, telemetry, started_ms, warnings)

        try:
            invocation = self._model.invoke_structured(
                EmailDraft,
                system_prompt=SYSTEM_PROMPT,
                user_prompt=build_compose_prompt(
                    state["normalized_lead"],
                    selection,
                    chunks,
                    nurturing_chunks=state.get("nurturing_chunks", []),
                    nurturing_route=state.get("nurturing_route"),
                ),
                temperature=0.3,
            )
            telemetry = add_usage(telemetry, invocation.usage)
            draft = invocation.value
        except Exception:
            telemetry = _record_failed_model_call(telemetry)
            warnings.append("Email composition provider failed.")
            result = self._result(
                state,
                status=GenerationStatus.PROVIDER_ERROR,
                telemetry=telemetry,
                warnings=warnings,
                strategy_references=chunks,
            )
            return self._finish_compose(result, telemetry, started_ms, warnings)

        if (
            draft.selected_product_family != selection.selected_product_family
            or draft.selected_application != selection.selected_application
        ):
            warnings.append("Email composition returned a mismatched catalog pair.")
            result = self._result(
                state,
                status=GenerationStatus.PROVIDER_ERROR,
                telemetry=telemetry,
                warnings=warnings,
                strategy_references=chunks,
            )
            return self._finish_compose(result, telemetry, started_ms, warnings)

        result = self._result(
            state,
            status=GenerationStatus.GENERATED,
            telemetry=telemetry,
            warnings=warnings,
            strategy_references=chunks,
            draft=draft,
        )
        return self._finish_compose(
            result, telemetry, started_ms, warnings, draft=draft
        )

    def _finish_compose(
        self,
        result: GenerationResult,
        telemetry: GenerationTelemetry,
        started_ms: int,
        warnings: list[str],
        *,
        draft: EmailDraft | None = None,
    ) -> dict[str, Any]:
        telemetry = record_node_timing(telemetry, "compose_email", started_ms)
        result = result.model_copy(update={"telemetry": telemetry})
        update: dict[str, Any] = {
            "result": result,
            "telemetry": telemetry,
            "warnings": warnings,
        }
        if draft is not None:
            update["draft"] = draft
        return update

    def _result(
        self,
        state: AgentState,
        *,
        status: GenerationStatus,
        telemetry: GenerationTelemetry,
        warnings: list[str],
        strategy_references: list[StrategyChunk],
        draft: EmailDraft | None = None,
    ) -> GenerationResult:
        selection = state["selection"]
        nurturing_route = state.get("nurturing_route")
        return GenerationResult(
            status=status,
            lead_id=state["normalized_lead"].lead_id,
            original_lead=deepcopy(state["original_lead"]),
            subject=draft.subject if draft else None,
            body=draft.body if draft else None,
            selected_product_family=selection.selected_product_family,
            selected_application=selection.selected_application,
            nurturing_email_number=(
                nurturing_route.email_number if nurturing_route is not None else None
            ),
            nurturing_email_theme=(
                nurturing_route.theme if nurturing_route is not None else None
            ),
            nurturing_kb_query=(
                nurturing_route.retrieval_query if nurturing_route is not None else None
            ),
            strategy_references=list(strategy_references),
            warnings=_deduplicate(warnings),
            prompt_version=PROMPT_VERSION,
            generated_at=_utc(self._clock()),
            telemetry=telemetry,
        )


def _normalize_selection(selection: ProductSelection) -> ProductSelection:
    if (
        selection.selection_status is SelectionStatus.LOW_CONFIDENCE
        or selection.confidence < MIN_SELECTION_CONFIDENCE
    ):
        return _low_confidence_selection(
            selection.selection_reason or "Gemini reported low confidence.",
            min(selection.confidence, MIN_SELECTION_CONFIDENCE - 0.01),
        )
    family = get_family(selection.selected_product_family)
    application = get_application(
        selection.selected_application,
        family_id=family.id if family else None,
    )
    if (
        family is None
        or application is None
        or not application_belongs_to_family(family.id, application.id)
    ):
        return _low_confidence_selection(
            "Gemini selection is not one canonical catalog pair.",
            min(selection.confidence, MIN_SELECTION_CONFIDENCE - 0.01),
        )
    return ProductSelection(
        selected_product_family=family.id,
        selected_application=application.id,
        confidence=selection.confidence,
        selection_reason=selection.selection_reason,
        retrieval_query=selection.retrieval_query,
        selection_status=SelectionStatus.SELECTED,
    )


def _low_confidence_selection(
    reason: str, confidence: float = 0.0
) -> ProductSelection:
    return ProductSelection(
        selected_product_family=None,
        selected_application=None,
        confidence=max(0.0, min(confidence, MIN_SELECTION_CONFIDENCE - 0.01)),
        selection_reason=reason,
        retrieval_query="",
        selection_status=SelectionStatus.LOW_CONFIDENCE,
    )


def _fallback_selection_from_hints(
    lead: NormalizedLead, hints: list[RoutingHint]
) -> ProductSelection:
    if not hints:
        return _low_confidence_selection(
            "Lead analysis provider was unavailable and no deterministic routing hint was available."
        )
    hint = hints[0]
    return ProductSelection(
        selected_product_family=hint.product_family,
        selected_application=hint.application,
        confidence=max(MIN_SELECTION_CONFIDENCE, 0.65),
        selection_reason=(
            "Deterministic fallback from routing hints after analysis provider failure."
        ),
        retrieval_query=_catalog_retrieval_query(lead, hint),
        selection_status=SelectionStatus.SELECTED,
    )


def _catalog_retrieval_query(lead: NormalizedLead, hint: RoutingHint) -> str:
    lead_terms = [
        lead.project,
        lead.section,
        lead.signal,
        lead.summary,
        lead.timing,
        lead.next_step,
    ]
    cleaned_terms = [
        term.strip()
        for term in lead_terms
        if isinstance(term, str) and term.strip()
    ]
    base = " ".join(cleaned_terms[:3]) if cleaned_terms else ""
    return (
        f"{hint.product_family} {hint.application} "
        f"{base}"
    ).strip()


def _fallback_nurturing_route(lead: NormalizedLead) -> NurturingRoute:
    stage = lead.project_stage
    if stage.name == "PLANNING":
        return NurturingRoute(
            email_number=1,
            theme="Design discovery and survey incentive",
            retrieval_query=_nurturing_template_query(1),
            rationale="Planning-stage leads are best served with discovery messaging first.",
        )
    if stage.name == "SPECIFICATION":
        return NurturingRoute(
            email_number=3,
            theme="Case study and proof of concept",
            retrieval_query=_nurturing_template_query(3),
            rationale="Specification-stage leads respond to case-study proof and implementation confidence.",
        )
    if stage.name == "PROCUREMENT":
        return NurturingRoute(
            email_number=6,
            theme="Specification readiness conversion push",
            retrieval_query=_nurturing_template_query(6),
            rationale="Procurement-stage leads are closer to conversion and need readiness-oriented CTAs.",
        )
    return NurturingRoute(
        email_number=2,
        theme="Performance and technical proof",
        retrieval_query=_nurturing_template_query(2),
        rationale="Unknown stage defaults to technical proof messaging before stronger conversion asks.",
    )


def _nurturing_template_query(email_number: int) -> str:
    templates = {
        1: "EMAIL 1 template structure subject preview body CTA survey incentive CEU tone section headings",
        2: "EMAIL 2 template structure subject preview body CTA technical proof performance data video resource tone section headings",
        3: "EMAIL 3 template structure subject preview body CTA case study proof of concept narrative tone section headings",
        4: "EMAIL 4 template structure subject preview body CTA support partnership objection handling tone section headings",
        5: "EMAIL 5 template structure subject preview body CTA objections deep dive benefit bullets tone section headings",
        6: "EMAIL 6 template structure subject preview body CTA specification readiness conversion push tone section headings",
        7: "EMAIL 7 template structure subject preview body CTA samples specs toolkit project kickoff tone section headings",
    }
    return templates.get(
        email_number,
        "EMAIL template structure subject preview body CTA tone section headings",
    )


def _record_failed_model_call(
    telemetry: GenerationTelemetry,
) -> GenerationTelemetry:
    return telemetry.model_copy(update={"model_calls": telemetry.model_calls + 1})


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = ["AccoyaEmailAgent"]
