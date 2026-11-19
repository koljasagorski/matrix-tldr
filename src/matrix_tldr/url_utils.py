"""Extraktion von https-Links aus Matrix-Nachrichten."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

# Alles ab "https://" bis zum nächsten Leer-/Steuer-/Klammerzeichen der Quelle.
_URL_RE = re.compile(r"https://[^\s<>\"'`\x00-\x1f\x7f]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?>\"'"
_MAX_URL_LENGTH = 2048
# Matrix-Permalinks (u. a. Mention-Pills ``https://matrix.to/#/@user:server``).
_MATRIX_LINK_HOSTS = frozenset({"matrix.to"})
_REPLY_HTML_RE = re.compile(r"<mx-reply>.*?</mx-reply>", re.IGNORECASE | re.DOTALL)


def _trim_trailing(url: str) -> str:
    """Schneidet Satzzeichen am Ende ab.

    ``)`` und ``]`` werden nur entfernt, wenn sie unausgeglichen sind. So bleibt
    ``https://de.wikipedia.org/wiki/Foo_(Bar)`` intakt, während
    ``(siehe https://example.org/a)`` korrekt gekürzt wird.
    """
    while url:
        last = url[-1]
        if last in _TRAILING_PUNCT:
            url = url[:-1]
        elif last == ")" and url.count(")") > url.count("("):
            url = url[:-1]
        elif last == "]" and url.count("]") > url.count("["):
            url = url[:-1]
        else:
            break
    return url


class _HrefParser(HTMLParser):
    """Sammelt href-Attribute aller ``<a>``-Tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.hrefs.append(value.strip())


def strip_reply_fallback(body: str, formatted_body: str | None) -> tuple[str, str | None]:
    """Entfernt Reply-Fallbacks, damit zitierte Links nicht erneut verarbeitet werden.

    Ältere Clients stellen einer Antwort die zitierte Nachricht voran
    (``> <@user> text``) bzw. ``<mx-reply>`` im HTML.
    """
    lines = body.split("\n")
    idx = 0
    while idx < len(lines) and lines[idx].startswith(">"):
        idx += 1
    if idx:
        if idx < len(lines) and not lines[idx].strip():
            idx += 1
        body = "\n".join(lines[idx:])
    if formatted_body:
        formatted_body = _REPLY_HTML_RE.sub("", formatted_body)
    return body, formatted_body


def extract_urls(
    body: str,
    formatted_body: str | None = None,
    *,
    max_links: int = 1,
) -> list[str]:
    """Extrahiert eindeutige https-Links in Reihenfolge des Auftretens.

    Args:
        body: Klartext der Nachricht.
        formatted_body: Optionales HTML; href-Links werden zusätzlich beachtet.
        max_links: Obergrenze der zurückgegebenen Links.

    Returns:
        Liste ohne Duplikate, höchstens ``max_links`` Einträge. http:// und
        andere Schemata werden ignoriert.
    """
    candidates: list[str] = [_trim_trailing(m.group()) for m in _URL_RE.finditer(body)]

    if formatted_body:
        parser = _HrefParser()
        try:
            parser.feed(formatted_body)
            parser.close()
        except Exception:
            pass
        candidates.extend(parser.hrefs)

    result: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if len(url) > _MAX_URL_LENGTH or not url.lower().startswith("https://"):
            continue
        try:
            parts = urlsplit(url)
            _ = parts.port  # löst ValueError bei ungültigem Port aus
        except ValueError:
            continue
        if not parts.hostname:
            continue
        if parts.hostname.lower() in _MATRIX_LINK_HOSTS:
            # Mentions, Raum- und Event-Permalinks sind keine Webseiten zum Zusammenfassen.
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
        if len(result) >= max_links:
            break
    return result


def display_domain(url: str) -> str:
    """Domain für die Anzeige (ohne führendes ``www.``)."""
    host = urlsplit(url).hostname or url
    return host.removeprefix("www.")
