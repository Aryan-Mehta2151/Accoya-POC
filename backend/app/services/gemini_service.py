"""Gemini LLM wrapper via LangChain."""
import json

from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import get_settings

settings = get_settings()


def _stringify_content(content: object) -> str:
    """Normalize model content into plain text for persistence and API output."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue

            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
                continue

            maybe_text = getattr(item, "text", None)
            if isinstance(maybe_text, str):
                parts.append(maybe_text)
            else:
                parts.append(str(item))

        return "\n".join(part for part in parts if part).strip()

    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        return json.dumps(content, ensure_ascii=False)

    return str(content)


def get_llm(temperature: float = 0.4) -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.gemini_api_key,
        temperature=temperature,
    )


def generate(prompt: str, temperature: float = 0.4) -> str:
    """Single-shot generation helper."""
    llm = get_llm(temperature=temperature)
    raw_content = llm.invoke(prompt).content
    return _stringify_content(raw_content)
