"""KI-Provider mit gemeinsamer Schnittstelle und Factory."""

from __future__ import annotations

from ..config import Settings
from .base import Summarizer, SummarizerError

__all__ = ["Summarizer", "SummarizerError", "get_summarizer"]


def get_summarizer(settings: Settings) -> Summarizer:
    """Erzeugt den Provider passend zu ``settings.ai_provider``.

    Modell, Keys und Base-URL kommen ausschließlich aus der Config.

    Raises:
        ValueError: Bei unbekanntem Provider (die Config validiert das bereits).
    """
    if settings.ai_provider == "anthropic":
        from .anthropic_provider import AnthropicSummarizer

        assert settings.anthropic_api_key  # von load_settings garantiert
        return AnthropicSummarizer(
            api_key=settings.anthropic_api_key,
            model=settings.ai_model,
            max_input_chars=settings.ai_max_input_chars,
            timeout_seconds=settings.ai_timeout_seconds,
            effort=settings.ai_effort,
        )
    if settings.ai_provider == "openai":
        from .openai_provider import OpenAISummarizer

        assert settings.openai_api_key  # von load_settings garantiert
        return OpenAISummarizer(
            api_key=settings.openai_api_key,
            model=settings.ai_model,
            max_input_chars=settings.ai_max_input_chars,
            timeout_seconds=settings.ai_timeout_seconds,
            base_url=settings.openai_base_url,
        )
    raise ValueError(f"Unbekannter AI_PROVIDER: {settings.ai_provider}")
