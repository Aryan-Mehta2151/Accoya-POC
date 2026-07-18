"""Offline integration tests for the compiled, bounded three-node graph."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from app.config import Settings
from agent.integrations import (
    KnowledgeBaseError,
    ModelInvocationError,
    StructuredInvocation,
)
from agent.models import (
    BenefitClaim,
    CTAType,
    EmailDraft,
    EmailDraftComponents,
    EvidenceReference,
    GenerationStatus,
    ProductSelection,
    SelectionStatus,
    StrategyChunk,
    TokenUsage,
    ValidationStatus,
)
from agent.policy import cta_text
from agent.workflow import AccoyaEmailAgent


FIXED_TIME = datetime(2026, 7, 17, 12, 30, tzinfo=timezone.utc)


def lead_record() -> dict:
    return {
        "id": "workflow-1",
        "Project": "Riverside Walkway",
        "Location": "Sacramento, CA",
        "Signal": "Thermory walkway is under planning review.",
        "Timing": "Early planning",
        "Contacts": "Taylor Smith - Project Architect, taylor@example.com",
        "Tags": ["Decking", "Planning"],
    }


def selected_product() -> ProductSelection:
    return ProductSelection(
        project_name="Riverside Walkway",
        project_stage="planning",
        project_application="Standard decking",
        material_signal="Thermory",
        named_competitor="Thermory",
        selected_product_family="accoya_wood",
        selected_application="standard_decking",
        selection_reason="The lead names Thermory and a walkway.",
        exact_source_trigger=EvidenceReference(
            source_type="lead",
            source_id="workflow-1",
            source_field="signal",
            quote="Thermory walkway",
        ),
        cta_type=CTAType.SAMPLE,
        benefit_topics=["stability"],
        retrieval_query="provider-suggested query",
        confidence=0.9,
    )


def low_confidence_selection() -> ProductSelection:
    return ProductSelection(
        confidence=0.2,
        selection_status=SelectionStatus.LOW_CONFIDENCE,
        selection_reason="No supported exterior application.",
        missing_information=["application"],
    )


def valid_draft(*, cite_strategy: bool = False) -> EmailDraft:
    sample_cta = cta_text("planning", CTAType.SAMPLE)
    body = (
        "Thermory walkway appears in the planning review for Riverside Walkway, creating "
        "a focused opportunity to evaluate Accoya Wood for standard decking. "
        "This draft stays with that single catalog application and does not "
        "assume a final decision. It remains centered on the supplied signal "
        "without adding unrelated project details, contacts, or technical claims.\n\n"
        "This note keeps the discussion centered on the supplied project signal "
        "and a practical next step for the planning stage. The purpose is simply "
        "to support evaluation of the relevant decking option while the project "
        f"team considers its needs. {sample_cta}"
    )
    strategy_quote = (
        "Approved positioning supports attractive appearance for decking applications."
    )
    if cite_strategy:
        body = body.replace("technical claims", "attractive appearance")
    return EmailDraft(
        subject="Accoya Wood for the Riverside walkway",
        body=body,
        selected_product_family="accoya_wood",
        selected_application="standard_decking",
        lead_evidence_used=[
            EvidenceReference(
                source_type="lead",
                source_id="workflow-1",
                source_field="signal",
                quote="Thermory walkway",
            ),
            EvidenceReference(
                source_type="lead",
                source_id="workflow-1",
                source_field="project",
                quote="Riverside Walkway",
            ),
            EvidenceReference(
                source_type="lead",
                source_id="workflow-1",
                source_field="signal",
                quote="planning review",
            ),
        ],
        strategy_source_ids=["strategy-1"] if cite_strategy else [],
        benefits=(
            [
                BenefitClaim(
                    topic="appearance",
                    claim="attractive appearance",
                    evidence=[
                        EvidenceReference(
                            source_type="strategy",
                            source_id="strategy-1",
                            quote=strategy_quote,
                        )
                    ],
                )
            ]
            if cite_strategy
            else []
        ),
        cta_type=CTAType.SAMPLE,
        cta_text=sample_cta,
        competitor_mentions=["Thermory"],
    )


def invalid_draft() -> EmailDraft:
    draft = valid_draft()
    return draft.model_copy(
        update={"body": "Thermory walkway. " + draft.cta_text}
    )


def draft_components(draft: EmailDraft) -> EmailDraftComponents:
    paragraphs = draft.body.split("\n\n") if draft.body else []
    return EmailDraftComponents(
        subject=draft.subject,
        opening_paragraph=paragraphs[0] if paragraphs else "",
        value_paragraph=paragraphs[1] if len(paragraphs) > 1 else "",
        closing_paragraph=paragraphs[2] if len(paragraphs) > 2 else None,
        selected_product_family=draft.selected_product_family,
        selected_application=draft.selected_application,
        lead_evidence_used=list(draft.lead_evidence_used),
        strategy_source_ids=list(draft.strategy_source_ids),
        benefits=list(draft.benefits),
        cta_type=draft.cta_type,
        cta_text=draft.cta_text,
        competitor_mentions=list(draft.competitor_mentions),
        warnings=list(draft.warnings),
    )


def approved_chunk() -> StrategyChunk:
    return StrategyChunk(
        document_id="strategy-1",
        text="Approved positioning supports attractive appearance for decking applications.",
        title="Decking strategy",
        metadata={"status": "approved"},
        score=0.92,
        source_location="s3://docs/decking.pdf",
    )


class FakeStructuredModel:
    model_name = "fake-gemini"

    def __init__(self, responses, events: list[str] | None = None):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.events = events

    def invoke_structured(
        self,
        schema,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ):
        self.calls.append(
            {
                "schema": schema,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
            }
        )
        if self.events is not None:
            self.events.append(f"model:{schema.__name__}")
        if not self.responses:
            raise AssertionError("Unexpected extra model call")
        expected_schema, response = self.responses.pop(0)
        if schema is not expected_schema:
            raise AssertionError(
                f"Expected {expected_schema.__name__}, got {schema.__name__}"
            )
        if isinstance(response, Exception):
            raise response
        return StructuredInvocation(
            value=response,
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )


class FakeRetriever:
    def __init__(self, chunks=(), error: Exception | None = None, events=None):
        self.chunks = list(chunks)
        self.error = error
        self.calls: list[dict] = []
        self.events = events

    def retrieve(self, query, *, top_k=5, metadata_filters=None):
        self.calls.append(
            {
                "query": query,
                "top_k": top_k,
                "metadata_filters": dict(metadata_filters or {}),
            }
        )
        if self.events is not None:
            self.events.append("retrieve")
        if self.error is not None:
            raise self.error
        return list(self.chunks)


def make_agent(model, retriever=None, **kwargs) -> AccoyaEmailAgent:
    return AccoyaEmailAgent(
        model=model,
        retriever=retriever,
        clock=lambda: FIXED_TIME,
        **kwargs,
    )


class WorkflowTests(unittest.TestCase):
    def test_compiled_graph_order_and_successful_generation_metadata(self):
        events: list[str] = []
        model = FakeStructuredModel(
            [
                (ProductSelection, selected_product()),
                (
                    EmailDraftComponents,
                    draft_components(valid_draft(cite_strategy=True)),
                ),
            ],
            events,
        )
        retriever = FakeRetriever([approved_chunk()], events=events)
        agent = make_agent(
            model,
            retriever,
            top_k=5,
            metadata_filters={"application": "standard_decking"},
        )
        original = lead_record()
        expected_original = deepcopy(original)

        result = agent.generate(original)

        self.assertEqual(
            events,
            [
                "model:ProductSelection",
                "retrieve",
                "model:EmailDraftComponents",
            ],
        )
        self.assertEqual(result.status, GenerationStatus.GENERATED)
        self.assertEqual(result.validation_status, ValidationStatus.VALID)
        self.assertIsNotNone(result.subject)
        self.assertIsNotNone(result.body)
        self.assertEqual(result.original_lead, expected_original)
        self.assertEqual(original, expected_original)
        self.assertEqual(result.generated_at, FIXED_TIME)
        self.assertEqual(result.selected_product_family, "accoya_wood")
        self.assertEqual(result.selected_application, "standard_decking")
        self.assertEqual(
            [item.document_id for item in result.strategy_references],
            ["strategy-1"],
        )

        self.assertEqual(retriever.calls[0]["top_k"], 5)
        self.assertEqual(
            retriever.calls[0]["metadata_filters"],
            {"application": "standard_decking"},
        )
        self.assertIn("Accoya Wood", retriever.calls[0]["query"])
        self.assertIn("planning", retriever.calls[0]["query"])
        self.assertEqual(result.telemetry.model_name, "fake-gemini")
        self.assertEqual(result.telemetry.model_calls, 2)
        self.assertEqual(result.telemetry.retrieval_count, 1)
        self.assertEqual(result.telemetry.retrieved_document_ids, ["strategy-1"])
        self.assertEqual(result.telemetry.token_usage.input_tokens, 20)
        self.assertEqual(result.telemetry.token_usage.output_tokens, 10)
        self.assertEqual(result.telemetry.token_usage.total_tokens, 30)
        self.assertFalse(result.telemetry.repair_attempted)
        self.assertGreaterEqual(result.telemetry.latency_ms, 0)
        self.assertEqual(
            set(result.telemetry.node_timings_ms),
            {"analyze_lead", "retrieve_strategy", "compose_and_validate"},
        )

        edges = {
            (edge.source, edge.target) for edge in agent.graph.get_graph().edges
        }
        self.assertEqual(
            edges,
            {
                ("__start__", "analyze_lead"),
                ("analyze_lead", "retrieve_strategy"),
                ("retrieve_strategy", "compose_and_validate"),
                ("compose_and_validate", "__end__"),
            },
        )

    def test_missing_retriever_uses_safe_catalog_fallback(self):
        model = FakeStructuredModel(
            [
                (ProductSelection, selected_product()),
                (EmailDraftComponents, draft_components(valid_draft())),
            ]
        )
        result = make_agent(model).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.GENERATED)
        self.assertEqual(result.strategy_references, [])
        self.assertTrue(any("not configured" in item for item in result.warnings))

    def test_retrieval_failure_or_unapproved_result_never_triggers_unfiltered_retry(self):
        scenarios = (
            (
                FakeRetriever(error=KnowledgeBaseError("denied")),
                "retrieval was unavailable",
            ),
            (
                FakeRetriever(error=OSError("unexpected transport error")),
                "retrieval was unavailable",
            ),
            (
                FakeRetriever(
                    [
                        approved_chunk().model_copy(
                            update={"metadata": {"status": "draft"}}
                        )
                    ]
                ),
                "No approved strategy",
            ),
        )
        for retriever, warning_text in scenarios:
            with self.subTest(warning_text=warning_text):
                model = FakeStructuredModel(
                    [
                        (ProductSelection, selected_product()),
                        (EmailDraftComponents, draft_components(valid_draft())),
                    ]
                )
                result = make_agent(model, retriever).generate(lead_record())
                self.assertEqual(result.status, GenerationStatus.GENERATED)
                self.assertEqual(len(retriever.calls), 1)
                self.assertEqual(result.telemetry.retrieval_count, 0)
                self.assertTrue(
                    any(warning_text.casefold() in item.casefold() for item in result.warnings)
                )

    def test_unrelated_low_confidence_lead_stops_before_retrieval_and_composition(self):
        model = FakeStructuredModel(
            [(ProductSelection, low_confidence_selection())]
        )
        retriever = FakeRetriever([approved_chunk()])
        record = {
            "id": "unrelated-1",
            "Project": "Interior lighting controls",
            "Summary": "Occupancy sensor upgrade.",
        }

        result = make_agent(model, retriever).generate(record)

        self.assertEqual(result.status, GenerationStatus.INSUFFICIENT_CONTEXT)
        self.assertIsNone(result.subject)
        self.assertIsNone(result.body)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(retriever.calls, [])

    def test_literal_bridge_and_gate_context_survives_generic_term_guard(self):
        scenarios = (
            (
                "bridge-1",
                "Replacement of an exterior timber bridge for a public trail.",
                "exterior timber bridge",
                "bridges",
            ),
            (
                "gate-1",
                "A new exterior wooden gate is under planning review.",
                "exterior wooden gate",
                "gates",
            ),
        )
        for lead_id, summary, trigger, application in scenarios:
            with self.subTest(application=application):
                selection = ProductSelection(
                    project_stage="planning",
                    selected_product_family="accoya_wood",
                    selected_application=application,
                    selection_reason="Literal exterior wood application.",
                    exact_source_trigger=EvidenceReference(
                        source_type="lead",
                        source_id=lead_id,
                        source_field="summary",
                        quote=trigger,
                    ),
                    cta_type=CTAType.SAMPLE,
                    confidence=0.9,
                )
                model = FakeStructuredModel(
                    [
                        (ProductSelection, selection),
                        (
                            EmailDraftComponents,
                            ModelInvocationError("stop after routing"),
                        ),
                    ]
                )
                retriever = FakeRetriever([])

                result = make_agent(model, retriever).generate(
                    {
                        "id": lead_id,
                        "Project": "Trail Improvements",
                        "Summary": summary,
                        "Timing": "Early planning",
                    }
                )

                self.assertEqual(result.status, GenerationStatus.PROVIDER_ERROR)
                self.assertEqual(result.selected_application, application)
                self.assertEqual(len(retriever.calls), 1)
                self.assertEqual(len(model.calls), 2)

    def test_generic_concrete_structure_does_not_force_catalog_application(self):
        selection = ProductSelection(
            project_stage="planning",
            selected_product_family="accoya_wood",
            selected_application="structures_sculptures",
            selection_reason="Generic structure noun.",
            exact_source_trigger=EvidenceReference(
                source_type="lead",
                source_id="structure-1",
                source_field="summary",
                quote="existing concrete structure",
            ),
            cta_type=CTAType.SAMPLE,
            confidence=0.9,
        )
        model = FakeStructuredModel([(ProductSelection, selection)])
        retriever = FakeRetriever([])

        result = make_agent(model, retriever).generate(
            {
                "id": "structure-1",
                "Project": "Electrical Controls",
                "Summary": (
                    "Inspection of existing concrete structure for electrical "
                    "controls."
                ),
                "Timing": "Early planning",
            }
        )

        self.assertEqual(result.status, GenerationStatus.INSUFFICIENT_CONTEXT)
        self.assertEqual(retriever.calls, [])
        self.assertEqual(len(model.calls), 1)

    def test_procurement_window_does_not_force_window_application(self):
        selection = ProductSelection(
            project_stage="procurement",
            selected_product_family="accoya_wood",
            selected_application="general_wooden_windows",
            selection_reason="Figurative window noun.",
            exact_source_trigger=EvidenceReference(
                source_type="lead",
                source_id="window-1",
                source_field="summary",
                quote="procurement window",
            ),
            cta_type=CTAType.SAMPLE,
            confidence=0.9,
        )
        model = FakeStructuredModel([(ProductSelection, selection)])
        retriever = FakeRetriever([])

        result = make_agent(model, retriever).generate(
            {
                "id": "window-1",
                "Project": "Electrical Controls",
                "Summary": "The procurement window closes next week.",
            }
        )

        self.assertEqual(result.status, GenerationStatus.INSUFFICIENT_CONTEXT)
        self.assertEqual(retriever.calls, [])
        self.assertEqual(len(model.calls), 1)

    def test_below_threshold_selected_response_becomes_insufficient_context(self):
        below_threshold = ProductSelection.model_validate(
            {**selected_product().model_dump(), "confidence": 0.5}
        )
        model = FakeStructuredModel([(ProductSelection, below_threshold)])
        retriever = FakeRetriever([approved_chunk()])

        result = make_agent(model, retriever).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.INSUFFICIENT_CONTEXT)
        self.assertIsNone(result.subject)
        self.assertIsNone(result.body)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(retriever.calls, [])

    def test_analysis_provider_failure_returns_safe_provider_error(self):
        model = FakeStructuredModel(
            [(ProductSelection, ModelInvocationError("analysis unavailable"))]
        )
        retriever = FakeRetriever([approved_chunk()])

        result = make_agent(model, retriever).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.PROVIDER_ERROR)
        self.assertIsNone(result.subject)
        self.assertIsNone(result.body)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(retriever.calls, [])
        self.assertEqual(result.telemetry.model_calls, 1)

    def test_from_settings_with_missing_gemini_key_returns_provider_result(self):
        settings = Settings(
            _env_file=None,
            gemini_api_key="",
            gemini_model="configured-model",
            bedrock_kb_id="",
        )
        result = AccoyaEmailAgent.from_settings(settings).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.PROVIDER_ERROR)
        self.assertIsNone(result.subject)
        self.assertIsNone(result.body)
        self.assertEqual(result.telemetry.model_name, "configured-model")

    def test_from_settings_degrades_when_bedrock_client_construction_fails(self):
        settings = Settings(
            _env_file=None,
            gemini_api_key="fake-key",
            gemini_model="configured-model",
            bedrock_kb_id="kb-configured",
        )
        model = FakeStructuredModel(
            [
                (ProductSelection, selected_product()),
                (EmailDraftComponents, draft_components(valid_draft())),
            ]
        )
        with patch(
            "agent.workflow.GeminiStructuredModel.from_settings",
            return_value=model,
        ), patch(
            "agent.workflow.BedrockStrategyRetriever.from_settings",
            side_effect=OSError("invalid AWS configuration"),
        ):
            result = AccoyaEmailAgent.from_settings(settings).generate(
                lead_record()
            )

        self.assertEqual(result.status, GenerationStatus.GENERATED)
        self.assertTrue(
            any("retrieval was unavailable" in item for item in result.warnings)
        )

    def test_composition_provider_failure_returns_safe_provider_error(self):
        model = FakeStructuredModel(
            [
                (ProductSelection, selected_product()),
                (
                    EmailDraftComponents,
                    ModelInvocationError("compose unavailable"),
                ),
            ]
        )

        result = make_agent(model).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.PROVIDER_ERROR)
        self.assertIsNone(result.subject)
        self.assertIsNone(result.body)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result.telemetry.model_calls, 2)

    def test_invalid_first_draft_runs_exactly_one_repair_and_can_succeed(self):
        model = FakeStructuredModel(
            [
                (ProductSelection, selected_product()),
                (EmailDraftComponents, draft_components(invalid_draft())),
                (EmailDraftComponents, draft_components(valid_draft())),
            ]
        )

        result = make_agent(model).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.GENERATED)
        self.assertEqual(len(model.calls), 3)
        self.assertTrue(result.telemetry.repair_attempted)
        repair_prompt = model.calls[2]["user_prompt"]
        self.assertIn('"task": "repair_once"', repair_prompt)
        self.assertIn("body_word_count", repair_prompt)
        self.assertIn("paragraph_count", repair_prompt)

    def test_blank_structured_fields_use_the_single_repair_pass(self):
        blank = valid_draft().model_copy(
            update={
                "subject": "",
                "body": "",
                "selected_product_family": "",
                "selected_application": "",
                "cta_text": "",
            }
        )
        model = FakeStructuredModel(
            [
                (ProductSelection, selected_product()),
                (EmailDraftComponents, draft_components(blank)),
                (EmailDraftComponents, draft_components(valid_draft())),
            ]
        )

        result = make_agent(model).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.GENERATED)
        self.assertEqual(len(model.calls), 3)
        self.assertTrue(result.telemetry.repair_attempted)

    def test_vacuous_analysis_trigger_becomes_insufficient_context(self):
        for field, quote in (
            ("signal", "planning"),
            ("timing", "Early planning"),
        ):
            with self.subTest(field=field, quote=quote):
                trivial = selected_product().model_copy(
                    update={
                        "exact_source_trigger": EvidenceReference(
                            source_type="lead",
                            source_id="workflow-1",
                            source_field=field,
                            quote=quote,
                        )
                    }
                )
                model = FakeStructuredModel([(ProductSelection, trivial)])
                retriever = FakeRetriever([approved_chunk()])

                result = make_agent(model, retriever).generate(lead_record())

                self.assertEqual(
                    result.status, GenerationStatus.INSUFFICIENT_CONTEXT
                )
                self.assertEqual(len(model.calls), 1)
                self.assertEqual(retriever.calls, [])

    def test_analysis_topics_are_bounded_before_retrieval(self):
        selection = selected_product().model_copy(
            update={
                "benefit_topics": [
                    "stability",
                    "appearance",
                    "maintenance",
                    "fourth-topic",
                    "fifth-topic",
                ]
            }
        )
        model = FakeStructuredModel(
            [
                (ProductSelection, selection),
                (EmailDraftComponents, draft_components(valid_draft())),
            ]
        )
        retriever = FakeRetriever([])

        result = make_agent(model, retriever).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.GENERATED)
        self.assertIn("stability", retriever.calls[0]["query"])
        self.assertNotIn("fourth-topic", retriever.calls[0]["query"])

    def test_analysis_discards_unparsed_material_and_competitor_values(self):
        selection = selected_product().model_copy(
            update={
                "material_signal": "planning",
                "named_competitor": "planning",
            }
        )
        model = FakeStructuredModel(
            [
                (ProductSelection, selection),
                (EmailDraftComponents, draft_components(valid_draft())),
            ]
        )
        retriever = FakeRetriever([])

        result = make_agent(model, retriever).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.GENERATED)
        query = retriever.calls[0]["query"]
        self.assertNotIn("material signal planning", query)
        self.assertNotIn("named competitor planning", query)

    def test_failed_repair_is_not_retried_and_invalid_draft_is_withheld(self):
        model = FakeStructuredModel(
            [
                (ProductSelection, selected_product()),
                (EmailDraftComponents, draft_components(invalid_draft())),
                (EmailDraftComponents, draft_components(invalid_draft())),
            ]
        )

        result = make_agent(model).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.VALIDATION_FAILED)
        self.assertEqual(result.validation_status, ValidationStatus.INVALID)
        self.assertIsNone(result.subject)
        self.assertIsNone(result.body)
        self.assertEqual(len(model.calls), 3)
        self.assertTrue(result.telemetry.repair_attempted)
        self.assertTrue(result.validation_violations)

    def test_repair_provider_failure_is_bounded_and_withholds_invalid_draft(self):
        model = FakeStructuredModel(
            [
                (ProductSelection, selected_product()),
                (EmailDraftComponents, draft_components(invalid_draft())),
                (
                    EmailDraftComponents,
                    ModelInvocationError("repair unavailable"),
                ),
            ]
        )

        result = make_agent(model).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.VALIDATION_FAILED)
        self.assertIsNone(result.subject)
        self.assertIsNone(result.body)
        self.assertEqual(len(model.calls), 3)
        self.assertTrue(result.telemetry.repair_attempted)
        self.assertTrue(any("repair pass failed" in item for item in result.warnings))


if __name__ == "__main__":
    unittest.main()
