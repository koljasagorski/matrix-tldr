"""Sicherer HTTP-Abruf und Textextraktion.

Der Abruf läuft komplett über unseren eigenen ``httpx``-Client mit
``PinnedTransport`` (SSRF-Schutz). trafilatura erhält nur den fertig geladenen
HTML-String und holt selbst nichts aus dem Netz.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura
from trafilatura.utils import decode_file

from .security import (
    BlockedURLError,
    HostResolutionError,
    PinnedTransport,
    Resolver,
)

logger = logging.getLogger(__name__)

USER_AGENT = "matrix-tldr/1.0 (+Matrix-Bot)"
MAX_REDIRECTS = 5
MIN_TEXT_CHARS = 300
ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
PAYWALL_STATUS_CODES = frozenset({401, 402, 403})
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
# Gesamtfrist relativ zum Einzel-Timeout. httpx-Timeouts gelten pro Netzwerkschritt,
# ein langsam tröpfelnder Server würde sie sonst beliebig strecken (Slowloris).
_TOTAL_TIMEOUT_FACTOR = 3


class FetchError(Exception):
    """Basisklasse aller Abruffehler."""


class FetchBlockedError(FetchError):
    """Ziel aus Sicherheitsgründen gesperrt (SSRF-Schutz)."""


class FetchTimeoutError(FetchError):
    """Zeitüberschreitung beim Abruf."""


class FetchConnectionError(FetchError):
    """Seite nicht erreichbar (DNS, Verbindung, TLS)."""


class FetchHTTPError(FetchError):
    """Server antwortete mit einem Fehlerstatus."""

    def __init__(self, status_code: int, message: str | None = None) -> None:
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


class UnsupportedContentError(FetchError):
    """Inhaltstyp ist kein HTML (PDF, Bild, Video, ...)."""


class ContentTooLargeError(FetchError):
    """Antwort überschreitet FETCH_MAX_BYTES."""


class NoContentError(FetchError):
    """Kein lesbarer Text vorhanden (leer, zu kurz oder vermutlich Paywall)."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Extrahierter Seiteninhalt."""

    url: str
    title: str | None
    text: str


def _decode(data: bytes, charset: str | None) -> str:
    if charset:
        try:
            codecs.lookup(charset)
        except LookupError:
            charset = None
    if charset:
        return data.decode(charset, errors="replace")
    return decode_file(data)


def _extract_sync(html: str) -> tuple[str | None, str | None]:
    """CPU-lastige Extraktion; läuft in einem Thread."""
    document = trafilatura.bare_extraction(
        html,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
        with_metadata=True,
    )
    if document is None:
        return None, None
    return document.text, document.title


class Fetcher:
    """Ruft Webseiten sicher ab und extrahiert den Haupttext."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_bytes: int,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Erzeugt den gemeinsamen HTTP-Client.

        Args:
            timeout_seconds: Timeout pro Netzwerkschritt (FETCH_TIMEOUT_SECONDS).
            max_bytes: Abbruchgrenze für die Antwortgröße (FETCH_MAX_BYTES).
            resolver: Optionaler DNS-Resolver (für Tests).
            transport: Optionaler Transport; Standard ist ``PinnedTransport``.
        """
        self._max_bytes = max_bytes
        self._total_timeout = timeout_seconds * _TOTAL_TIMEOUT_FACTOR
        self._client = httpx.AsyncClient(
            transport=transport or PinnedTransport(resolver=resolver),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,  # Redirects werden manuell verfolgt und geprüft
            trust_env=False,  # Proxy-Umgebungsvariablen würden das Pinning umgehen
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "de,en",
            },
        )

    async def aclose(self) -> None:
        """Schließt den HTTP-Client."""
        await self._client.aclose()

    async def fetch(self, url: str) -> FetchResult:
        """Lädt ``url`` und extrahiert Titel und Haupttext.

        Raises:
            FetchBlockedError: SSRF-Schutz hat das Ziel gesperrt.
            FetchTimeoutError: Zeitüberschreitung.
            FetchConnectionError: Seite nicht erreichbar.
            FetchHTTPError: Fehlerstatus des Servers.
            UnsupportedContentError: Kein HTML.
            ContentTooLargeError: Antwort zu groß.
            NoContentError: Kein lesbarer Text bzw. vermutlich Paywall.
        """
        try:
            async with asyncio.timeout(self._total_timeout):
                html, final_url = await self._download(url)
        except TimeoutError as exc:
            raise FetchTimeoutError("Gesamtzeit überschritten") from exc

        text, title = await asyncio.to_thread(_extract_sync, html)
        text = (text or "").strip()
        if len(text) < MIN_TEXT_CHARS:
            raise NoContentError(
                f"nur {len(text)} Zeichen extrahiert (vermutlich Paywall oder leer)"
            )
        return FetchResult(url=final_url, title=(title or "").strip() or None, text=text)

    async def _download(self, url: str) -> tuple[str, str]:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                redirect_to, body = await self._fetch_hop(current)
            except BlockedURLError as exc:
                raise FetchBlockedError(str(exc)) from exc
            except HostResolutionError as exc:
                raise FetchConnectionError(str(exc)) from exc
            except httpx.TimeoutException as exc:
                raise FetchTimeoutError(str(exc) or "Timeout") from exc
            except httpx.HTTPError as exc:
                raise FetchConnectionError(f"{type(exc).__name__}: {exc}") from exc
            if redirect_to is None:
                assert body is not None
                return body, current
            current = redirect_to
        raise FetchHTTPError(310, f"mehr als {MAX_REDIRECTS} Weiterleitungen")

    async def _fetch_hop(self, url: str) -> tuple[str | None, str | None]:
        """Ein einzelner Request (zustandslos, daher nebenläufig sicher).

        Returns:
            ``(redirect_ziel, None)`` bei einer Weiterleitung, sonst
            ``(None, dekodierter_body)``.
        """
        async with self._client.stream("GET", url) as response:
            status = response.status_code
            if status in _REDIRECT_STATUS_CODES:
                location = response.headers.get("location")
                if not location:
                    raise FetchHTTPError(status, "Redirect ohne Location")
                target = urljoin(url, location)
                if urlsplit(target).scheme.lower() != "https":
                    raise FetchBlockedError("Redirect auf Nicht-https-Ziel")
                return target, None
            if status in PAYWALL_STATUS_CODES:
                raise NoContentError(f"HTTP {status} (vermutlich Paywall)")
            if status >= 400:
                raise FetchHTTPError(status)

            content_type = response.headers.get("content-type", "")
            mime = content_type.split(";", 1)[0].strip().lower()
            if mime not in ALLOWED_CONTENT_TYPES:
                raise UnsupportedContentError(f"Inhaltstyp {mime or 'unbekannt'}")

            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self._max_bytes:
                raise ContentTooLargeError(f"Content-Length {declared} > Limit")

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():  # dekodierte Bytes zählen
                total += len(chunk)
                if total > self._max_bytes:
                    raise ContentTooLargeError(f"mehr als {self._max_bytes} Bytes")
                chunks.append(chunk)
            return None, _decode(b"".join(chunks), response.charset_encoding)
