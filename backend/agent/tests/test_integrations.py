"""Tests for folder-local provider adapters without making live calls."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from agent.integrations import (
    BedrockStrategyRetriever,
    GeminiStructuredModel,
    KnowledgeBaseError,
    ModelInvocationError,
    _token_usage,
)
from agent.models import EmailDraft


class FakeBedrockClient:
    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self.response = response or {"retrievalResults": []}
        self.error = error
        self.calls: list[dict] = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class BedrockStrategyRetrieverTests(unittest.TestCase):
    def test_request_has_no_filter_and_every_result_is_mapped(self):
        client = FakeBedrockClient(
            {
                "retrievalResults": [
                    {
                        "documentId": "doc-approved",
                        "content": {"text": "Approved positioning."},
                        "metadata": {
                            "status": "APPROVED",
                            "title": "Decking strategy",
                            "application": "standard_decking",
                        },
                        "score": 0.91,
                        "location": {"s3Location": {"uri": "s3://docs/decking.pdf"}},
                    },
                    {
                        "documentId": "doc-draft",
                        "content": {"text": "Aspirational draft."},
                        "metadata": {"status": "draft"},
                        "score": 0.99,
                    },
                    {
                        "documentId": "doc-arbitrary",
                        "content": {"text": "Arbitrary metadata is retained."},
                        "metadata": {
                            "status": "anything",
                            "application": "standard_siding",
                        },
                        "score": "provider-score",
                    },
                    {
                        "documentId": "doc-empty",
                        "content": {"text": "  "},
                    },
                ]
            }
        )
        retriever = BedrockStrategyRetriever(
            client=client, knowledge_base_id="kb-123"
        )

        chunks = retriever.retrieve(
            "Accoya decking planning architect",
            top_k=5,
        )

        self.assertEqual(len(chunks), 4)
        self.assertEqual(chunks[0].document_id, "doc-approved")
        self.assertEqual(chunks[0].title, "Decking strategy")
        self.assertEqual(chunks[0].score, 0.91)
        self.assertEqual(
            chunks[0].source_location,
            {"s3Location": {"uri": "s3://docs/decking.pdf"}},
        )
        self.assertEqual(len(client.calls), 1)
        vector = client.calls[0]["retrievalConfiguration"][
            "vectorSearchConfiguration"
        ]
        self.assertEqual(vector["numberOfResults"], 5)
        self.assertNotIn("filter", vector)
        self.assertEqual(chunks[1].metadata, {"status": "draft"})
        self.assertEqual(chunks[2].score, "provider-score")
        self.assertEqual(chunks[3].text, "  ")
        self.assertEqual(chunks[3].metadata, {})

    def test_derives_stable_document_id_and_title_when_provider_omits_them(self):
        response = {
            "retrievalResults": [
                {
                    "content": {"text": "Catalog-aligned strategy."},
                    "metadata": {"status": "approved"},
                    "location": {
                        "s3Location": {"uri": "s3://docs/strategy-one.txt?version=1"}
                    },
                }
            ]
        }
        first = BedrockStrategyRetriever(
            client=FakeBedrockClient(response), knowledge_base_id="kb"
        ).retrieve("query")
        second = BedrockStrategyRetriever(
            client=FakeBedrockClient(response), knowledge_base_id="kb"
        ).retrieve("query")

        self.assertTrue(first[0].document_id.startswith("derived-"))
        self.assertEqual(first[0].document_id, second[0].document_id)
        self.assertEqual(first[0].title, "strategy-one.txt")

    def test_empty_query_returns_without_calling_provider(self):
        client = FakeBedrockClient()
        retriever = BedrockStrategyRetriever(client=client, knowledge_base_id="kb")

        self.assertEqual(retriever.retrieve("  "), [])
        self.assertEqual(client.calls, [])

    def test_provider_failure_is_wrapped_once(self):
        client = FakeBedrockClient(error=RuntimeError("denied"))
        retriever = BedrockStrategyRetriever(client=client, knowledge_base_id="kb")

        with self.assertRaisesRegex(KnowledgeBaseError, "retrieval failed"):
            retriever.retrieve("query")

        self.assertEqual(len(client.calls), 1)

    def test_rejects_missing_configuration_and_nonpositive_top_k(self):
        with self.assertRaisesRegex(ValueError, "BEDROCK_KB_ID"):
            BedrockStrategyRetriever(client=FakeBedrockClient(), knowledge_base_id=" ")

        retriever = BedrockStrategyRetriever(
            client=FakeBedrockClient(), knowledge_base_id="kb"
        )
        for top_k in (0, -1):
            with self.subTest(top_k=top_k), self.assertRaisesRegex(
                ValueError, "positive"
            ):
                retriever.retrieve("query", top_k=top_k)


class GeminiStructuredModelTests(unittest.TestCase):
    def test_request_timeout_is_validated_and_forwarded(self):
        with self.assertRaisesRegex(ValueError, "timeout must be positive"):
            GeminiStructuredModel(
                model_name="fake-model",
                api_key="fake-key",
                request_timeout_seconds=0,
            )

        llm = MagicMock()
        runnable = MagicMock()
        runnable.invoke.return_value = {
            "parsed": EmailDraft(
                subject="Subject",
                body="Body",
                selected_product_family="Decking",
                selected_application="Standard decking",
            ),
            "raw": None,
        }
        llm.with_structured_output.return_value = runnable
        model = GeminiStructuredModel(
            model_name="fake-model",
            api_key="fake-key",
            request_timeout_seconds=12.5,
        )

        with patch(
            "agent.integrations.ChatGoogleGenerativeAI",
            return_value=llm,
        ) as constructor:
            model.invoke_structured(
                EmailDraft,
                system_prompt="system",
                user_prompt="user",
                temperature=0.1,
            )

        constructor.assert_called_once_with(
            model="fake-model",
            google_api_key="fake-key",
            temperature=0.1,
            request_timeout=12.5,
        )

    def test_setup_errors_are_wrapped_as_model_invocation_errors(self):
        model = GeminiStructuredModel(model_name="fake-model", api_key="fake-key")
        with patch(
            "agent.integrations.ChatGoogleGenerativeAI",
            side_effect=RuntimeError("setup failed"),
        ), self.assertRaisesRegex(ModelInvocationError, "invocation failed"):
            model.invoke_structured(
                EmailDraft,
                system_prompt="system",
                user_prompt="user",
                temperature=0.1,
            )

    def test_missing_or_malformed_usage_remains_unknown(self):
        missing = _token_usage(None)
        self.assertIsNone(missing.input_tokens)
        self.assertIsNone(missing.output_tokens)
        self.assertIsNone(missing.total_tokens)

        malformed = _token_usage(
            {"input_tokens": "bad", "output_tokens": -2, "total_tokens": object()}
        )
        self.assertIsNone(malformed.input_tokens)
        self.assertIsNone(malformed.output_tokens)
        self.assertIsNone(malformed.total_tokens)


if __name__ == "__main__":
    unittest.main()
