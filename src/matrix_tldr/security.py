"""SSRF-Schutz (sicherheitskritisch).

Der Bot ruft URLs ab, die beliebige Chat-Teilnehmer posten. Ohne Schutz könnte
jemand den Bot dazu bringen, interne Dienste (Router, Proxmox, Cloud-Metadaten)
abzufragen. Die Verteidigung besteht aus drei Schichten:

1. **Validierung**: Nur ``https`` und Port 443, keine Zugangsdaten in der URL,
   keine internen Hostnamen. IP-Literale werden inklusive Sonderschreibweisen
   (dezimal, oktal, hexadezimal, verkürzt) normalisiert und geprüft.
2. **DNS-Prüfung**: ALLE Ergebnisse von ``getaddrinfo`` (IPv4 und IPv6) müssen
   öffentlich sein. Genügt eine einzige interne Adresse, wird blockiert.
3. **Pinning gegen DNS-Rebinding**: ``PinnedTransport`` löst auf, prüft und
   verbindet sich anschließend direkt mit der geprüften IP. Auflösung und
   Verbindung sind damit ein atomarer Schritt, ein zweiter DNS-Lookup mit
   anderem Ergebnis ist ausgeschlossen. SNI und ``Host``-Header bleiben beim
   Hostnamen, die TLS-Zertifikatsprüfung läuft daher weiter gegen den Namen.

Redirects werden im ``fetcher`` manuell verfolgt; jeder Hop läuft erneut durch
diesen Transport und damit durch alle drei Schichten.

Restrisiko: Der Schutz setzt voraus, dass der Bot-Host keinen Proxy erzwingt
(``trust_env=False`` im Fetcher) und dass ausgehende Verbindungen nicht über
eine Firewall-Regel auf interne Ziele umgeleitet werden. Als zusätzliche
Absicherung sollte der Container im Netzwerk keinen Zugriff auf
interne Segmente haben (siehe README).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]

DEFAULT_ALLOWED_PORTS: frozenset[int] = frozenset({443})
_DNS_TIMEOUT_SECONDS = 5.0
_MAX_CONNECT_ATTEMPTS = 3
_MAX_URL_LENGTH = 2048

# Explizite Blockliste, ergänzt die ipaddress-Eigenschaften (is_private usw.).
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(net)
    for net in (
        # IPv4
        "0.0.0.0/8",  # "dieses" Netz, unspezifiziert
        "10.0.0.0/8",  # privat
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",  # Loopback
        "169.254.0.0/16",  # Link-Local inkl. Cloud-Metadata 169.254.169.254
        "172.16.0.0/12",  # privat
        "192.0.0.0/24",  # IETF-Protokollzuweisungen
        "192.0.2.0/24",  # Dokumentation
        "192.168.0.0/16",  # privat
        "198.18.0.0/15",  # Benchmarking
        "198.51.100.0/24",  # Dokumentation
        "203.0.113.0/24",  # Dokumentation
        "224.0.0.0/4",  # Multicast
        "240.0.0.0/4",  # reserviert, inkl. Broadcast
        # IPv6
        "::/8",  # unspezifiziert, Loopback, IPv4-kompatibel, reserviert
        "100::/64",  # Discard-Präfix
        "2001::/23",  # IETF-Zuweisungen inkl. Teredo
        "2001:db8::/32",  # Dokumentation
        "2002::/16",  # 6to4 (bettet beliebige IPv4 ein)
        "64:ff9b::/96",  # NAT64 (bettet beliebige IPv4 ein)
        "64:ff9b:1::/48",
        "fc00::/7",  # Unique Local
        "fe80::/10",  # Link-Local
        "fec0::/10",  # Site-Local (veraltet)
        "ff00::/8",  # Multicast
    )
)

_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
    ".localdomain",
)

# Ein Label, das wie ein Teil einer (Sonder-)IPv4-Schreibweise aussieht.
_NUMERIC_LABEL = re.compile(r"^(?:0[xX][0-9a-fA-F]*|[0-9]+)$")


class BlockedURLError(Exception):
    """Die URL darf aus Sicherheitsgründen nicht abgerufen werden."""


class HostResolutionError(Exception):
    """Der Hostname ließ sich nicht auflösen."""


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    """Ergebnis einer erfolgreichen Prüfung."""

    scheme: str
    host: str
    port: int
    ips: tuple[str, ...]


def _parse_legacy_ipv4(host: str) -> ipaddress.IPv4Address:
    """Parst IPv4-Sonderschreibweisen mit ``inet_aton``-Semantik.

    Unterstützt ``2130706433``, ``0x7f000001``, ``0177.0.0.1``, ``127.1`` usw.

    Raises:
        ValueError: Bei ungültigen Zahlen (z. B. ``08`` im Oktalsystem).
    """
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        raise ValueError("zu viele Segmente")
    numbers: list[int] = []
    for part in parts:
        if not part:
            raise ValueError("leeres Segment")
        if part[:2].lower() == "0x":
            numbers.append(int(part[2:] or "0", 16))
        elif len(part) > 1 and part.startswith("0"):
            numbers.append(int(part[1:], 8))
        else:
            numbers.append(int(part, 10))
    *head, last = numbers
    if any(n > 255 for n in head):
        raise ValueError("Segment > 255")
    remaining_bytes = 5 - len(parts)
    if last >= 256**remaining_bytes:
        raise ValueError("letztes Segment zu groß")
    value = 0
    for n in head:
        value = (value << 8) | n
    value = (value << (8 * remaining_bytes)) | last
    return ipaddress.IPv4Address(value)


def parse_ip_literal(host: str) -> IPAddress | None:
    """Erkennt IP-Literale und normalisiert sie.

    Args:
        host: Hostteil einer URL (ohne Klammern bei IPv6).

    Returns:
        Die normalisierte Adresse, oder ``None``, wenn ``host`` ein gewöhnlicher
        Domainname ist.

    Raises:
        BlockedURLError: Wenn der Host wie eine IP aussieht, sich aber nicht
            eindeutig parsen lässt (fail closed).
    """
    candidate = host.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if ":" in candidate:
        try:
            return ipaddress.IPv6Address(candidate.split("%", 1)[0])
        except ValueError as exc:
            raise BlockedURLError(f"ungültige IPv6-Adresse: {host!r}") from exc
    labels = candidate.rstrip(".").split(".")
    if all(_NUMERIC_LABEL.match(label) for label in labels):
        try:
            return _parse_legacy_ipv4(candidate.rstrip("."))
        except ValueError as exc:
            raise BlockedURLError(f"ungültige numerische Adresse: {host!r}") from exc
    return None


def blocked_reason(ip: IPAddress) -> str | None:
    """Prüft eine IP-Adresse.

    Returns:
        Begründung, falls die Adresse blockiert werden muss, sonst ``None``.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 vor der Prüfung entpacken
    for network in _BLOCKED_NETWORKS:
        if ip.version == network.version and ip in network:
            return f"in gesperrtem Netz {network}"
    if ip.is_loopback:
        return "Loopback"
    if ip.is_private:
        return "privat"
    if ip.is_link_local:
        return "Link-Local"
    if ip.is_multicast:
        return "Multicast"
    if ip.is_unspecified:
        return "unspezifiziert"
    if ip.is_reserved:
        return "reserviert"
    if not ip.is_global:
        return "nicht global routbar"
    return None


async def system_resolver(host: str, port: int) -> list[str]:
    """Löst einen Hostnamen über den Systemresolver auf (IPv4 und IPv6)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=_DNS_TIMEOUT_SECONDS,
        )
    except (socket.gaierror, TimeoutError, OSError) as exc:
        raise HostResolutionError(f"Auflösung fehlgeschlagen: {host}") from exc
    return [str(info[4][0]) for info in infos]


async def validate_host(
    scheme: str,
    host: str,
    port: int,
    *,
    allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
    resolver: Resolver | None = None,
) -> ValidatedTarget:
    """Prüft Schema, Port und Ziel-Adressen.

    Raises:
        BlockedURLError: Wenn das Ziel blockiert werden muss.
        HostResolutionError: Wenn der Name nicht auflösbar ist.
    """
    if scheme.lower() != "https":
        raise BlockedURLError(f"Schema {scheme!r} nicht erlaubt (nur https)")
    if port not in allowed_ports:
        raise BlockedURLError(f"Port {port} nicht erlaubt")
    if not host:
        raise BlockedURLError("kein Host")

    literal = parse_ip_literal(host)
    if literal is not None:
        reason = blocked_reason(literal)
        if reason:
            raise BlockedURLError(f"IP {literal} blockiert: {reason}")
        return ValidatedTarget("https", str(literal), port, (str(literal),))

    try:
        ascii_host = host.rstrip(".").lower().encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise BlockedURLError(f"ungültiger Hostname: {host!r}") from exc
    if (
        ascii_host == "localhost"
        or ascii_host.endswith(_BLOCKED_HOST_SUFFIXES)
        or "." not in ascii_host  # Einzel-Label: wird über Suchdomänen intern aufgelöst
    ):
        raise BlockedURLError(f"interner Hostname blockiert: {ascii_host}")

    addresses = await (resolver or system_resolver)(ascii_host, port)
    if not addresses:
        raise HostResolutionError(f"keine Adressen für {ascii_host}")

    checked: list[str] = []
    for raw in dict.fromkeys(addresses):  # Reihenfolge erhalten, Duplikate entfernen
        try:
            ip = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError as exc:
            raise BlockedURLError(f"unlesbare Adresse {raw!r} für {ascii_host}") from exc
        reason = blocked_reason(ip)
        if reason:  # EINE interne Adresse genügt für die Blockade
            raise BlockedURLError(f"{ascii_host} löst auf {ip} auf: {reason}")
        checked.append(str(ip))
    return ValidatedTarget("https", ascii_host, port, tuple(checked))


async def validate_url(
    url: str,
    *,
    allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
    resolver: Resolver | None = None,
) -> ValidatedTarget:
    """Prüft eine vollständige URL (siehe ``validate_host``).

    Raises:
        BlockedURLError: Bei unzulässigen URLs.
        HostResolutionError: Wenn der Hostname nicht auflösbar ist.
    """
    if len(url) > _MAX_URL_LENGTH or re.search(r"[\s\x00-\x1f\x7f]", url):
        raise BlockedURLError("URL enthält unzulässige Zeichen oder ist zu lang")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise BlockedURLError("URL nicht parsebar") from exc
    if parts.username is not None or parts.password is not None:
        raise BlockedURLError("Zugangsdaten in der URL nicht erlaubt")
    return await validate_host(
        parts.scheme,
        host or "",
        port or 443,
        allowed_ports=allowed_ports,
        resolver=resolver,
    )


class PinnedTransport(httpx.AsyncBaseTransport):
    """httpx-Transport, der pro Request auflöst, prüft und auf die IP pinnt.

    Jeder Request (also auch jeder Redirect-Hop) durchläuft ``validate_host``.
    Verbunden wird mit der geprüften IP. ``Host``-Header und TLS-SNI bleiben
    der ursprüngliche Hostname, damit die Zertifikatsprüfung gegen den Namen
    läuft. Es gibt keinen zweiten DNS-Lookup, der ein anderes Ergebnis liefern
    könnte (DNS-Rebinding).
    """

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._resolver = resolver
        self._allowed_ports = allowed_ports
        self._inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Validiert das Ziel und leitet den Request an die geprüfte IP weiter."""
        raw_host = request.url.raw_host.decode("ascii")
        target = await validate_host(
            request.url.scheme,
            raw_host,
            request.url.port or 443,
            allowed_ports=self._allowed_ports,
            resolver=self._resolver,
        )
        last_error: Exception | None = None
        for ip in target.ips[:_MAX_CONNECT_ATTEMPTS]:
            extensions = dict(request.extensions)
            extensions["sni_hostname"] = target.host
            pinned = httpx.Request(
                request.method,
                request.url.copy_with(host=ip),
                headers=request.headers,  # enthält den Host-Header des Originals
                stream=request.stream,
                extensions=extensions,
            )
            try:
                return await self._inner.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def aclose(self) -> None:
        """Schließt den inneren Transport."""
        await self._inner.aclose()
