"""OpenAI-Provider (auch für OpenAI-kompatible Endpunkte über ``OPENAI_BASE_URL``)."""

from __future__ import annotations

import logging

import openai
from openai import AsyncOpenAI

from .base import SYSTEM_PROMPT, SummarizerError, build_user_message, postprocess_summary

logger = logging.getLogger(__name__)

# Der SDK-Client wiederholt 429/5xx und Verbindungsfehler selbst mit Backoff.
_MAX_RETRIES = 2


class OpenAISummarizer:
    """Fasst Webseitentext mit einem OpenAI-kompatiblen Chat-Modell zusammen."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_input_chars: int,
        timeout_seconds: float,
        base_url: str | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        """Erzeugt den Provider.

        Args:
            api_key: API-Key (aus der Config).
            model: Modellname (AI_MODEL).
            max_input_chars: Maximale Textlänge, die ans Modell gesendet wird.
            timeout_seconds: Timeout pro Request.
            base_url: Optionaler Endpunkt für OpenAI-kompatible Server.
            client: Optional vorbereiteter Client (Tests).
        """
        self._model = model
        self._max_input_chars = max_input_chars
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=_MAX_RETRIES,
        )

    async def summarize(self, text: str, title: str | None, url: str) -> str:
        """Siehe ``Summarizer.summarize``."""
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": build_user_message(text, title, url, self._max_input_chars),
                    },
                ],
            )
        except openai.APIError as exc:
            status = getattr(exc, "status_code", None)
            raise SummarizerError(f"OpenAI-API-Fehler ({type(exc).__name__}, {status})") from exc

        if not response.choices:
            raise SummarizerError("Antwort ohne Choices")
        choice = response.choices[0]
        if choice.finish_reason in {"content_filter", "length"}:
            raise SummarizerError(f"unbrauchbare Antwort (finish_reason={choice.finish_reason})")
        return postprocess_summary(choice.message.content or "")

    async def aclose(self) -> None:
        """Schließt den HTTP-Client."""
        await self._client.close()
