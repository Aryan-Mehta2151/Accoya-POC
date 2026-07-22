"""RAG orchestration: retrieve from Bedrock KB, then answer with Gemini."""
from app.services import bedrock_service, gemini_service

_QA_SYSTEM = (
    "You are a helpful assistant for a marketing team. Answer questions about the "
    "company's strategy documents and the outreach emails that were sent. "
    "Use ONLY the provided context. If the answer is not in the context, say you "
    "don't have that information."
)


_ROLE_LABELS = {"human": "User", "ai": "Assistant"}


def answer_question(question: str, history: list[dict] | None = None) -> tuple[str, list[str]]:
    """Retrieve relevant context and generate a grounded answer.

    Only the current question is sent to Bedrock retrieval. The prior
    conversation history is included solely in the Gemini prompt.

    Returns (answer, source_list).
    """
    chunks = bedrock_service.retrieve(question)
    context = "\n\n---\n\n".join(c.text for c in chunks)
    sources = sorted({c.source for c in chunks if c.source})

    history_block = ""
    if history:
        history_text = "\n".join(
            f"{_ROLE_LABELS.get(m['role'], m['role'])}: {m['content']}"
            for m in history
        )
        history_block = f"Conversation so far:\n{history_text}\n\n"

    prompt = (
        f"{_QA_SYSTEM}\n\n"
        f"{history_block}"
        f"Context:\n{context}\n\n"
        f"Question: {question}\n\n"
        f"Answer:"
    )
    answer = gemini_service.generate(prompt, temperature=0.2)
    return answer, sources
