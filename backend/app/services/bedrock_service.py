"""Bedrock Knowledge Base helpers for retrieval and direct QnA generation."""
from dataclasses import dataclass

from app.config import get_settings
from app.services.aws import bedrock_agent_runtime_client

settings = get_settings()


class BedrockKnowledgeBaseError(RuntimeError):
    """Raised when Bedrock KB calls fail."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class RetrievedChunk:
    text: str
    source: str
    score: float


@dataclass
class KbAnswer:
    answer: str
    sources: list[str]
    session_id: str | None


def retrieve(query: str, top_k: int | None = None) -> list[RetrievedChunk]:
    """Return the most relevant chunks from the knowledge base for a query."""
    client = bedrock_agent_runtime_client()
    response = client.retrieve(
        knowledgeBaseId=settings.bedrock_kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": top_k or settings.bedrock_kb_top_k
            }
        },
    )

    chunks: list[RetrievedChunk] = []
    for result in response.get("retrievalResults", []):
        location = result.get("location", {})
        source = (
            location.get("s3Location", {}).get("uri")
            or location.get("type", "unknown")
        )
        chunks.append(
            RetrievedChunk(
                text=result.get("content", {}).get("text", ""),
                source=source,
                score=result.get("score", 0.0),
            )
        )
    return chunks


def retrieve_and_generate(question: str, session_id: str | None = None) -> KbAnswer:
    """Ask Bedrock KB directly, matching the AWS console test behavior.

    Uses RetrieveAndGenerate with the configured knowledge base and model ARN.
    """
    if not settings.bedrock_kb_id:
        raise BedrockKnowledgeBaseError("BEDROCK_KB_ID is not set")
    if not settings.bedrock_kb_model_arn:
        raise BedrockKnowledgeBaseError("BEDROCK_KB_MODEL_ARN is not set")

    model_arn = _normalize_model_arn(settings.bedrock_kb_model_arn, settings.aws_region)

    client = bedrock_agent_runtime_client()

    kwargs = {
        "input": {"text": question},
        "retrieveAndGenerateConfiguration": {
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": settings.bedrock_kb_id,
                "modelArn": model_arn,
                "retrievalConfiguration": {
                    "vectorSearchConfiguration": {
                        "numberOfResults": settings.bedrock_kb_top_k
                    }
                },
            },
        },
    }
    if session_id:
        kwargs["sessionId"] = session_id

    try:
        response = client.retrieve_and_generate(**kwargs)
    except Exception as exc:
        raise BedrockKnowledgeBaseError(f"Bedrock KB generate failed: {exc}") from exc

    answer = response.get("output", {}).get("text", "")
    sources: list[str] = []
    for citation in response.get("citations", []):
        for ref in citation.get("retrievedReferences", []):
            location = ref.get("location", {})
            source = (
                location.get("s3Location", {}).get("uri")
                or location.get("type")
            )
            if source:
                sources.append(source)

    deduped_sources = sorted(set(sources))
    return KbAnswer(
        answer=answer,
        sources=deduped_sources,
        session_id=response.get("sessionId") or session_id,
    )


def _normalize_model_arn(model_ref: str, region: str) -> str:
    """Accept either full ARN or model ID and return a full Bedrock model ARN."""
    value = (model_ref or "").strip()
    if value.startswith("arn:"):
        return value
    # Model ID example: anthropic.claude-3-5-sonnet-20241022-v2:0
    return f"arn:aws:bedrock:{region}::foundation-model/{value}"
