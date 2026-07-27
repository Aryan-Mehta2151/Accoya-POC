"""Offline tests for the simplified, bounded workflow."""

from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from app.config import Settings
from agent.integrations import ModelInvocationError, StructuredInvocation
from agent.models import (
    EmailDraft,
    GenerationStatus,
    NurturingRoute,
    ProductSelection,
    SelectionStatus,
    StrategyChunk,
    TokenUsage,
)
from agent.observability import log_result
from agent.workflow import AccoyaEmailAgent


FIXED_TIME = datetime(2026, 7, 17, 12, 30, tzinfo=timezone.utc)


def lead_record():
    return {
        "id": "workflow-1",
        "Project": "Riverside Walkway",
        "Location": "Sacramento, CA",
        "Signal": "Thermory walkway is under planning review.",
        "Timing": "Early planning",
    }


def selection(**updates):
    data = {
        "selected_product_family": "accoya_wood",
        "selected_application": "standard_decking",
        "confidence": 0.9,
        "selection_reason": "Decking match",
        "retrieval_query": "Accoya standard decking Thermory walkway",
        "selection_status": SelectionStatus.SELECTED,
    }
    data.update(updates)
    return ProductSelection(**data)


def low_selection(confidence=0.2):
    return ProductSelection(
        confidence=confidence,
        selection_reason="No credible catalog match",
        selection_status=SelectionStatus.LOW_CONFIDENCE,
    )


def draft(**updates):
    data = {
        "subject": "Accoya Wood for Riverside Walkway",
        "body": (
            "The Riverside Walkway lead is evaluating Thermory for its decking. "
            "Accoya Wood may be worth considering for the standard decking application.\n\n"
            "Would a short conversation about the material options be useful?"
        ),
        "selected_product_family": "accoya_wood",
        "selected_application": "standard_decking",
    }
    data.update(updates)
    return EmailDraft(**data)


def nurturing_route(**updates):
    data = {
        "email_number": 4,
        "theme": "Partnership and support",
        "retrieval_query": "model-proposed template query",
        "rationale": "The lead is moving toward commitment.",
    }
    data.update(updates)
    return NurturingRoute(**data)


def chunk(document_id="doc-1", metadata=None):
    return StrategyChunk(
        document_id=document_id,
        text="Raw KB positioning context.",
        title="Strategy",
        metadata=metadata if metadata is not None else {"status": "draft"},
        score="raw-score",
        source_location={"s3Location": {"uri": "s3://docs/strategy.txt"}},
    )


class FakeStructuredModel:
    model_name = "fake-gemini"

    def __init__(self, responses, events=None):
        self.responses = list(responses)
        self.calls = []
        self.events = events

    def invoke_structured(self, schema, *, system_prompt, user_prompt, temperature):
        self.calls.append({
            "schema": schema,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "temperature": temperature,
        })
        if self.events is not None:
            self.events.append(f"model:{schema.__name__}")
        if not self.responses:
            raise AssertionError("Unexpected model call")
        expected, value = self.responses.pop(0)
        self.assert_schema(expected, schema)
        if isinstance(value, Exception):
            raise value
        return StructuredInvocation(
            value=value,
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )

    @staticmethod
    def assert_schema(expected, actual):
        if expected is not actual:
            raise AssertionError(f"Expected {expected.__name__}, got {actual.__name__}")


class FakeRetriever:
    def __init__(self, chunks=(), error=None, events=None, responses=None):
        self.chunks = list(chunks)
        self.error = error
        self.events = events
        self.responses = list(responses) if responses is not None else None
        self.calls = []

    def retrieve(self, query, *, top_k=5):
        self.calls.append({"query": query, "top_k": top_k})
        if self.events is not None:
            self.events.append("retrieve")
        if self.responses is not None:
            if not self.responses:
                raise AssertionError("Unexpected retrieval call")
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return list(response)
        if self.error:
            raise self.error
        return list(self.chunks)


def make_agent(model, retriever=None, **kwargs):
    return AccoyaEmailAgent(
        model=model,
        retriever=retriever,
        clock=lambda: FIXED_TIME,
        **kwargs,
    )


class WorkflowTests(unittest.TestCase):
    def test_successful_nurturing_order_top_k_and_combined_telemetry(self):
        events = []
        model = FakeStructuredModel(
            [
                (ProductSelection, selection()),
                (NurturingRoute, nurturing_route()),
                (EmailDraft, draft()),
            ],
            events,
        )
        strategy_chunks = [chunk("strategy-1"), chunk("strategy-2", {})]
        nurturing_chunks = [chunk("nurturing-1")]
        retriever = FakeRetriever(
            events=events,
            responses=[strategy_chunks, nurturing_chunks],
        )

        result = make_agent(model, retriever, top_k=7).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.GENERATED)
        self.assertEqual(
            events,
            [
                "model:ProductSelection",
                "retrieve",
                "model:NurturingRoute",
                "retrieve",
                "model:EmailDraft",
            ],
        )
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(
            [call["top_k"] for call in retriever.calls],
            [7, 7],
        )
        self.assertEqual(
            retriever.calls[0]["query"], selection().retrieval_query
        )
        self.assertIn("EMAIL 4", retriever.calls[1]["query"])
        self.assertNotEqual(
            retriever.calls[1]["query"], nurturing_route().retrieval_query
        )
        self.assertEqual(result.strategy_references, strategy_chunks)
        self.assertEqual(result.nurturing_email_number, 4)
        self.assertEqual(result.nurturing_email_theme, "Partnership and support")
        self.assertEqual(result.telemetry.model_calls, 3)
        self.assertEqual(result.telemetry.retrieval_count, 3)
        self.assertEqual(
            result.telemetry.retrieved_document_ids,
            ["strategy-1", "strategy-2", "nurturing-1"],
        )
        self.assertEqual(
            set(result.telemetry.node_timings_ms),
            {"analyze_lead", "retrieve_strategy", "compose_email"},
        )
        self.assertEqual(result.generated_at, FIXED_TIME)

    def test_alias_pair_is_canonicalized_with_confidence_only(self):
        alias = selection(
            selected_product_family="Accoya Wood",
            selected_application="decking",
        )
        model = FakeStructuredModel(
            [
                (ProductSelection, alias),
                (NurturingRoute, nurturing_route()),
                (EmailDraft, draft()),
            ]
        )
        result = make_agent(model).generate(lead_record())
        self.assertEqual(result.status, GenerationStatus.GENERATED)
        self.assertEqual(result.selected_product_family, "accoya_wood")
        self.assertEqual(result.selected_application, "standard_decking")

    def test_low_below_threshold_and_invalid_catalog_generate_best_effort(self):
        cases = (
            low_selection(),
            selection(confidence=0.59),
            selection(selected_product_family="invented"),
            selection(selected_application="exterior_mdf_panels"),
        )
        for proposed in cases:
            with self.subTest(selection=proposed.model_dump()):
                model = FakeStructuredModel([
                    (ProductSelection, proposed),
                    (EmailDraft, draft()),
                ])
                retriever = FakeRetriever([chunk()])
                result = make_agent(model, retriever).generate(lead_record())
                self.assertEqual(result.status, GenerationStatus.GENERATED)
                self.assertEqual(retriever.calls, [])
                self.assertEqual(len(model.calls), 2)
                self.assertEqual(result.telemetry.retrieval_count, 0)
                self.assertTrue(
                    any("limited_context_best_effort" in item for item in result.warnings)
                )

    def test_missing_empty_or_failed_retrieval_still_generates(self):
        scenarios = (
            (None, "not configured"),
            (FakeRetriever([]), "no results"),
            (FakeRetriever(error=RuntimeError("denied")), "retrieval failed"),
        )
        for retriever, warning in scenarios:
            with self.subTest(warning=warning):
                model = FakeStructuredModel(
                    [
                        (ProductSelection, selection()),
                        (NurturingRoute, nurturing_route()),
                        (EmailDraft, draft()),
                    ]
                )
                result = make_agent(model, retriever).generate(lead_record())
                self.assertEqual(result.status, GenerationStatus.GENERATED)
                self.assertEqual(result.strategy_references, [])
                self.assertTrue(
                    any(warning in item.casefold() for item in result.warnings)
                )

    def test_draft_pair_mismatch_is_provider_error_without_retry(self):
        model = FakeStructuredModel([
            (ProductSelection, selection()),
            (NurturingRoute, nurturing_route()),
            (EmailDraft, draft(selected_application="pool_decking")),
        ])
        raw_chunks = [chunk()]
        result = make_agent(model, FakeRetriever(raw_chunks)).generate(lead_record())
        self.assertEqual(result.status, GenerationStatus.PROVIDER_ERROR)
        self.assertIsNone(result.subject)
        self.assertEqual(result.strategy_references, raw_chunks)
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(result.telemetry.model_calls, 3)
        self.assertTrue(any("mismatched" in item for item in result.warnings))

    def test_terminal_analysis_failure_skips_all_later_external_work(self):
        analysis_model = FakeStructuredModel([
            (ProductSelection, ModelInvocationError("analysis unavailable"))
        ])
        retriever = FakeRetriever([chunk()])
        analysis = make_agent(analysis_model, retriever).generate(
            {"id": "unroutable-lead"}
        )
        self.assertEqual(analysis.status, GenerationStatus.PROVIDER_ERROR)
        self.assertEqual(analysis.telemetry.model_calls, 1)
        self.assertEqual(analysis.telemetry.retrieval_count, 0)
        self.assertEqual(retriever.calls, [])
        self.assertEqual(len(analysis_model.calls), 1)
        self.assertIsNone(analysis.nurturing_email_number)

    def test_analysis_provider_failure_with_hint_uses_fallback_selection(self):
        model = FakeStructuredModel([
            (ProductSelection, ModelInvocationError("analysis unavailable")),
            (NurturingRoute, nurturing_route()),
            (EmailDraft, draft()),
        ])
        retriever = FakeRetriever(responses=[[chunk("strategy")], []])

        result = make_agent(model, retriever).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.GENERATED)
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(len(retriever.calls), 2)
        self.assertTrue(
            any("deterministic routing fallback" in item for item in result.warnings)
        )

    def test_composition_provider_failure_is_safe(self):
        compose_model = FakeStructuredModel([
            (ProductSelection, selection()),
            (NurturingRoute, nurturing_route()),
            (EmailDraft, ModelInvocationError("composition unavailable")),
        ])
        composition = make_agent(compose_model, FakeRetriever([chunk()])).generate(
            lead_record()
        )
        self.assertEqual(composition.status, GenerationStatus.PROVIDER_ERROR)
        self.assertIsNone(composition.body)
        self.assertEqual(composition.telemetry.model_calls, 3)

    def test_nurturing_route_failure_uses_stage_fallback(self):
        model = FakeStructuredModel([
            (ProductSelection, selection()),
            (NurturingRoute, ModelInvocationError("route unavailable")),
            (EmailDraft, draft()),
        ])
        retriever = FakeRetriever(
            responses=[[chunk("strategy")], [chunk("nurturing")]]
        )

        result = make_agent(model, retriever).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.GENERATED)
        self.assertEqual(result.nurturing_email_number, 1)
        self.assertIn("EMAIL 1", result.nurturing_kb_query)
        self.assertIn("EMAIL 1", retriever.calls[1]["query"])
        self.assertEqual(result.telemetry.model_calls, 3)
        self.assertTrue(
            any("fallback route" in item for item in result.warnings)
        )

    def test_each_retrieval_failure_is_independently_nonterminal(self):
        scenarios = (
            (
                [RuntimeError("strategy denied"), [chunk("nurturing")]],
                "Knowledge Base retrieval failed",
                ["nurturing"],
            ),
            (
                [[chunk("strategy")], RuntimeError("nurturing denied")],
                "Nurturing template retrieval failed",
                ["strategy"],
            ),
        )
        for responses, warning, expected_ids in scenarios:
            with self.subTest(warning=warning):
                model = FakeStructuredModel([
                    (ProductSelection, selection()),
                    (NurturingRoute, nurturing_route()),
                    (EmailDraft, draft()),
                ])
                result = make_agent(
                    model, FakeRetriever(responses=responses)
                ).generate(lead_record())
                self.assertEqual(result.status, GenerationStatus.GENERATED)
                self.assertEqual(
                    result.telemetry.retrieved_document_ids, expected_ids
                )
                self.assertEqual(result.telemetry.retrieval_count, 1)
                self.assertTrue(any(warning in item for item in result.warnings))

    def test_subject_has_no_agent_length_limit(self):
        long_subject = "S" * 5000
        model = FakeStructuredModel([
            (ProductSelection, selection()),
            (NurturingRoute, nurturing_route()),
            (EmailDraft, draft(subject=long_subject)),
        ])

        result = make_agent(model).generate(lead_record())

        self.assertEqual(result.status, GenerationStatus.GENERATED)
        self.assertEqual(result.subject, long_subject)

    def test_structured_log_omits_lead_derived_and_email_content(self):
        model = FakeStructuredModel([
            (ProductSelection, selection()),
            (NurturingRoute, nurturing_route()),
            (EmailDraft, draft()),
        ])
        result = make_agent(model).generate(lead_record())

        with patch("agent.observability.logger.info") as log_info:
            log_result(result)

        extra = log_info.call_args.kwargs["extra"]
        self.assertEqual(extra["generation_status"], "generated")
        self.assertEqual(extra["nurturing_email_number"], 4)
        for forbidden in (
            "retrieval_query",
            "original_lead",
            "subject",
            "body",
            "chunk_text",
        ):
            self.assertNotIn(forbidden, extra)

    def test_original_record_is_deeply_preserved(self):
        record = lead_record()
        record["nested"] = {"values": [1, 2]}
        expected = deepcopy(record)
        model = FakeStructuredModel(
            [
                (ProductSelection, selection()),
                (NurturingRoute, nurturing_route()),
                (EmailDraft, draft()),
            ]
        )
        result = make_agent(model).generate(record)
        record["nested"]["values"].append(3)
        self.assertEqual(result.original_lead, expected)

    def test_from_settings_missing_gemini_returns_provider_error(self):
        settings = Settings(
            _env_file=None,
            gemini_api_key="",
            gemini_model="configured-model",
            bedrock_kb_id="",
        )
        result = AccoyaEmailAgent.from_settings(settings).generate(
            {"id": "unroutable-lead"}
        )
        self.assertEqual(result.status, GenerationStatus.PROVIDER_ERROR)
        self.assertEqual(result.telemetry.model_name, "configured-model")
        self.assertEqual(result.telemetry.model_calls, 1)

    def test_from_settings_bedrock_construction_failure_is_fallback(self):
        settings = Settings(
            _env_file=None,
            gemini_api_key="fake-key",
            gemini_model="configured-model",
            bedrock_kb_id="kb",
        )
        model = FakeStructuredModel(
            [
                (ProductSelection, selection()),
                (NurturingRoute, nurturing_route()),
                (EmailDraft, draft()),
            ]
        )
        with patch(
            "agent.workflow.GeminiStructuredModel.from_settings",
            return_value=model,
        ), patch(
            "agent.workflow.BedrockStrategyRetriever.from_settings",
            side_effect=OSError("bad AWS config"),
        ):
            result = AccoyaEmailAgent.from_settings(settings).generate(lead_record())
        self.assertEqual(result.status, GenerationStatus.GENERATED)
        self.assertTrue(any("retrieval failed" in item for item in result.warnings))


if __name__ == "__main__":
    unittest.main()
