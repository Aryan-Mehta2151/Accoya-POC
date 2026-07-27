"""Tests for Bedrock-grounded chat orchestration."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import rag_service
from app.services.bedrock_service import RetrievedChunk


class RagServiceTests(unittest.TestCase):
    @patch("app.services.rag_service.gemini_service.generate")
    @patch("app.services.rag_service.bedrock_service.retrieve")
    def test_content_question_uses_bedrock_and_gemini(
        self,
        mock_retrieve,
        mock_generate,
    ) -> None:
        mock_retrieve.return_value = [
            RetrievedChunk(
                text="AIA and CEU guidance appears in section 3.",
                source="s3://kb/doc-a.md",
                score=0.98,
            )
        ]
        mock_generate.return_value = "AIA and CEU are covered in section 3."

        answer, sources = rag_service.answer_question(
            "is there anything in docs about aia or ceu course?",
            history=[{"role": "human", "content": "Earlier turn"}],
        )

        self.assertEqual(answer, "AIA and CEU are covered in section 3.")
        self.assertEqual(sources, ["s3://kb/doc-a.md"])
        mock_retrieve.assert_called_once_with(
            "is there anything in docs about aia or ceu course?"
        )
        mock_generate.assert_called_once()

    @patch("app.services.rag_service.gemini_service.generate")
    @patch("app.services.rag_service.bedrock_service.retrieve")
    def test_greeting_also_uses_bedrock_and_gemini(
        self,
        mock_retrieve,
        mock_generate,
    ) -> None:
        mock_retrieve.return_value = [
            RetrievedChunk(
                text="Greeting guidance from KB context.",
                source="s3://kb/doc-b.md",
                score=0.42,
            )
        ]
        mock_generate.return_value = "Hello! How can I help with your docs?"

        answer, sources = rag_service.answer_question("hello")

        self.assertEqual(answer, "Hello! How can I help with your docs?")
        self.assertEqual(sources, ["s3://kb/doc-b.md"])
        mock_retrieve.assert_called_once_with("hello")
        mock_generate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
