"""Claude-Provider (Anthropic Messages API)."""

from __future__ import annotations

import logging

import anthropic
from anthropic import AsyncAnthropic

from .base import SYSTEM_PROMPT, SummarizerError, build_user_message, postprocess_summary

logger = logging.getLogger(__name__)

# Reicht großzügig für zwei Sätze; Thinking-Tokens zählen ebenfalls gegen dieses
# Limit, deshalb nicht knapp bemessen (bei effort=low wird kaum nachgedacht).
_MAX_OUTPUT_TOKENS = 1024
# Der SDK-Client wiederholt 408/409/429/5xx und Verbindungsfehler selbst mit
# exponentiellem Backoff. Ein zusätzlicher eigener Retry würde die Versuche
# multiplizieren, deshalb wird hier ausschließlich der SDK-Mechanismus genutzt.
_MAX_RETRIES = 2


class AnthropicSummarizer:
    """Fasst Webseitentext mit einem Claude-Modell zusammen."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_input_chars: int,
        timeout_seconds: float,
        effort: str | None = "low",
        client: AsyncAnthropic | None = None,
    ) -> None:
        """Erzeugt den Provider.

        Args:
            api_key: Anthropic-API-Key (aus der Config).
            model: Modellname (AI_MODEL, nie im Code hartkodiert).
            max_input_chars: Maximale Textlänge, die ans Modell gesendet wird.
            timeout_seconds: Timeout pro Request.
            effort: Denkaufwand (``low``..``max``); ``None`` sendet keinen Wert,
                nötig für Modelle ohne Effort-Unterstützung.
            client: Optional vorbereiteter Client (Tests).
        """
        self._model = model
        self._max_input_chars = max_input_chars
        self._effort = effort
        self._client = client or AsyncAnthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=_MAX_RETRIES
        )

    async def summarize(self, text: str, title: str | None, url: str) -> str:
        """Siehe ``Summarizer.summarize``."""
        # Kein temperature/top_p: aktuelle Modelle lehnen abweichende Werte ab.
        params: dict[str, object] = {
            "model": self._model,
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": build_user_message(text, title, url, self._max_input_chars),
                }
            ],
        }
        if self._effort:
            params["output_config"] = {"effort": self._effort}

        try:
            response = await self._client.messages.create(**params)  # type: ignore[arg-type]
        except anthropic.APIError as exc:
            # Nur Typ und Status loggen, kein Inhalt und keine Header.
            status = getattr(exc, "status_code", None)
            raise SummarizerError(f"Anthropic-API-Fehler ({type(exc).__name__}, {status})") from exc

        if response.stop_reason in {"refusal", "max_tokens"}:
            raise SummarizerError(f"unbrauchbare Antwort (stop_reason={response.stop_reason})")
        raw = "".join(block.text for block in response.content if block.type == "text")
        return postprocess_summary(raw)

    async def aclose(self) -> None:
        """Schließt den HTTP-Client."""
        await self._client.close()
