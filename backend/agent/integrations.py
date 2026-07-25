"""Folder-local Gemini and Amazon Bedrock Knowledge Base integrations.

The agent deliberately depends only on :mod:`app.config` from the existing
application.  No legacy email, RAG, model, AWS, or Bedrock service is imported.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import logging
import time
from pathlib import PurePosixPath
from typing import Any, Generic, Mapping, Protocol, TypeVar

import boto3
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel

from app.config import Settings, get_settings

from .models import StrategyChunk, TokenUsage


logger = logging.getLogger(__name__)

StructuredT = TypeVar("StructuredT", bound=BaseModel)


def _is_transient_error(exc: Exception) -> bool:
    """Return True for retryable provider overload/rate-limit errors."""
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if status in (429, 500, 503):
        return True
    text = str(exc).upper()
    return (
        "503" in text
        or "429" in text
        or "UNAVAILABLE" in text
        or "RESOURCE_EXHAUSTED" in text
        or "OVERLOADED" in text
        or "HIGH DEMAND" in text
    )


class ModelInvocationError(RuntimeError):
    """Raised when Gemini fails or does not return the requested schema."""


class KnowledgeBaseError(RuntimeError):
    """Raised when approved Knowledge Base retrieval cannot be completed."""


@dataclass(frozen=True)
class StructuredInvocation(Generic[StructuredT]):
    """A parsed model response and its reported token usage."""

    value: StructuredT
    usage: TokenUsage


class StructuredModel(Protocol):
    """Minimal interface consumed by the graph and implemented by test fakes."""

    @property
    def model_name(self) -> str:
        """Return the configured provider model name."""

    def invoke_structured(
        self,
        schema: type[StructuredT],
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> StructuredInvocation[StructuredT]:
        """Generate and parse one response against ``schema``."""


class StrategyRetriever(Protocol):
    """Minimal unfiltered Knowledge Base retrieval interface."""

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
    ) -> list[StrategyChunk]:
        """Return raw Knowledge Base chunks for ``query``."""


class UnavailableStructuredModel:
    """Configured model placeholder that produces a safe provider outcome."""

    def __init__(self, *, model_name: str, reason: str) -> None:
        self._model_name = model_name.strip() or "unconfigured"
        self._reason = reason

    @property
    def model_name(self) -> str:
        return self._model_name

    def invoke_structured(
        self,
        schema: type[StructuredT],
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> StructuredInvocation[StructuredT]:
        del schema, system_prompt, user_prompt, temperature
        raise ModelInvocationError(self._reason)


class GeminiStructuredModel:
    """LangChain Gemini adapter with Pydantic structured output."""

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str,
        request_timeout_seconds: float | None = None,
    ) -> None:
        if not model_name.strip():
            raise ValueError("GEMINI_MODEL is not configured")
        if not api_key.strip():
            raise ValueError("GEMINI_API_KEY is not configured")
        if request_timeout_seconds is not None and request_timeout_seconds <= 0:
            raise ValueError("Gemini request timeout must be positive")
        self._model_name = model_name.strip()
        self._api_key = api_key.strip()
        self._request_timeout_seconds = request_timeout_seconds

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "GeminiStructuredModel":
        configured = settings or get_settings()
        return cls(
            model_name=configured.gemini_model,
            api_key=configured.gemini_api_key,
            request_timeout_seconds=configured.gemini_request_timeout_seconds,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    def invoke_structured(
        self,
        schema: type[StructuredT],
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> StructuredInvocation[StructuredT]:
        try:
            llm = ChatGoogleGenerativeAI(
                model=self._model_name,
                google_api_key=self._api_key,
                temperature=temperature,
                request_timeout=self._request_timeout_seconds,
            )
            runnable = llm.with_structured_output(schema, include_raw=True)
            response = self._invoke_with_retry(
                runnable,
                [("system", system_prompt), ("human", user_prompt)],
            )
            parsed = response.get("parsed") if isinstance(response, Mapping) else None
            parsing_error = (
                response.get("parsing_error")
                if isinstance(response, Mapping)
                else None
            )
            if parsed is None:
                detail = str(parsing_error or "no parsed structured output")
                raise ModelInvocationError(
                    f"Gemini structured output failed: {detail}"
                )
            if not isinstance(parsed, schema):
                parsed = schema.model_validate(parsed)
            raw_message = (
                response.get("raw") if isinstance(response, Mapping) else None
            )
            usage = _token_usage(getattr(raw_message, "usage_metadata", None))
        except ModelInvocationError:
            raise
        except Exception as exc:  # provider exceptions vary by SDK release
            logger.error(
                "Gemini call failed (model=%s): %s", self._model_name, exc
            )
            raise ModelInvocationError(f"Gemini invocation failed: {exc}") from exc
        return StructuredInvocation(value=parsed, usage=usage)

    def _invoke_with_retry(
        self,
        runnable: Any,
        messages: list[tuple[str, str]],
        *,
        max_attempts: int = 4,
    ) -> Any:
        """Invoke the model, retrying transient 503/429 overload errors."""
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return runnable.invoke(messages)
            except Exception as exc:  # noqa: BLE001 - inspect message for retryable codes
                if not _is_transient_error(exc) or attempt == max_attempts:
                    raise
                last_exc = exc
                delay = min(2 ** (attempt - 1), 8)
                logger.warning(
                    "Gemini transient error (model=%s, attempt %d/%d); "
                    "retrying in %ds: %s",
                    self._model_name,
                    attempt,
                    max_attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)
        # Unreachable: loop either returns or raises.
        raise last_exc if last_exc else RuntimeError("retry loop failed")



class BedrockStrategyRetriever:
    """Direct, unfiltered Bedrock Knowledge Base ``Retrieve`` adapter."""

    def __init__(self, *, client: Any, knowledge_base_id: str) -> None:
        if not knowledge_base_id.strip():
            raise ValueError("BEDROCK_KB_ID is not configured")
        self._client = client
        self._knowledge_base_id = knowledge_base_id.strip()

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None
    ) -> "BedrockStrategyRetriever":
        configured = settings or get_settings()
        session = boto3.Session(
            region_name=configured.aws_region,
            aws_access_key_id=configured.aws_access_key_id or None,
            aws_secret_access_key=configured.aws_secret_access_key or None,
        )
        return cls(
            client=session.client("bedrock-agent-runtime"),
            knowledge_base_id=configured.bedrock_kb_id,
        )

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
    ) -> list[StrategyChunk]:
        cleaned_query = query.strip()
        if not cleaned_query:
            return []
        if top_k < 1:
            raise ValueError("top_k must be positive")

        vector_configuration: dict[str, Any] = {
            "numberOfResults": top_k,
        }
        try:
            response = self._client.retrieve(
                knowledgeBaseId=self._knowledge_base_id,
                retrievalQuery={"text": cleaned_query},
                retrievalConfiguration={
                    "vectorSearchConfiguration": vector_configuration,
                },
            )
        except Exception as exc:  # botocore exception types vary by operation
            raise KnowledgeBaseError(f"Bedrock KB retrieval failed: {exc}") from exc

        chunks: list[StrategyChunk] = []
        for result in response.get("retrievalResults", []):
            metadata = dict(result.get("metadata") or {})
            text = str((result.get("content") or {}).get("text") or "")
            location = dict(result.get("location") or {})
            source_uri = _source_location(location)
            document_id = str(result.get("documentId") or "")
            if not document_id:
                digest_input = f"{source_uri}\n{text}".encode("utf-8")
                document_id = f"derived-{sha256(digest_input).hexdigest()[:24]}"

            title = _document_title(metadata, source_uri, document_id)
            chunks.append(
                StrategyChunk(
                    document_id=document_id,
                    text=text,
                    title=title,
                    metadata=metadata,
                    score=result.get("score"),
                    source_location=location or None,
                )
            )
        return chunks


def _source_location(location: Mapping[str, Any]) -> str:
    for details in location.values():
        if not isinstance(details, Mapping):
            continue
        for key in ("uri", "url", "webLocation", "filePath"):
            value = details.get(key)
            if value:
                return str(value)
    if location:
        return json.dumps(location, sort_keys=True, default=str)
    return "unknown"


def _document_title(
    metadata: Mapping[str, Any], source_location: str, document_id: str
) -> str:
    for key in ("title", "document_title", "filename", "name"):
        value = metadata.get(key)
        if value:
            return str(value)
    if source_location not in {"", "unknown"}:
        candidate = PurePosixPath(source_location.split("?", 1)[0]).name
        if candidate:
            return candidate
    return document_id


def _token_usage(value: Mapping[str, Any] | None) -> TokenUsage:
    usage = value if isinstance(value, Mapping) else {}
    input_tokens = _optional_token_count(usage.get("input_tokens"))
    output_tokens = _optional_token_count(usage.get("output_tokens"))
    total_tokens = _optional_token_count(usage.get("total_tokens"))
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)
    input_details = usage.get("input_token_details")
    cached_input_tokens = (
        _optional_token_count(input_details.get("cache_read"))
        if isinstance(input_details, Mapping)
        else None
    )
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
    )


def _optional_token_count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None
