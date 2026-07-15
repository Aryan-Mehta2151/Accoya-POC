"""Gemini LLM wrapper via LangChain."""
from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import get_settings

settings = get_settings()


def get_llm(temperature: float = 0.4) -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.gemini_api_key,
        temperature=temperature,
    )


def generate(prompt: str, temperature: float = 0.4) -> str:
    """Single-shot generation helper."""
    llm = get_llm(temperature=temperature)
    return llm.invoke(prompt).content
