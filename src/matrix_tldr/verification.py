"""Interaktive SAS-Emoji-Geräteverifizierung.

Hintergrund: matrix-nio kennt nur die Ereignisse ``start``, ``accept``, ``key``,
``mac`` und ``cancel``. Aktuelle Clients (Element) beginnen eine Verifizierung
aber mit ``m.key.verification.request`` und erwarten ein ``ready`` sowie am Ende
ein ``done``. Diese drei Nachrichten ergänzt dieser Handler selbst (nio liefert
``request`` als ``UnknownToDeviceEvent``). Der eigentliche SAS-Ablauf bleibt bei nio.

Sicherheitsmodell: Verifizierungen werden ausschließlich für Sessions des
eigenen Bot-Kontos akzeptiert (Anmeldung des Bot-Kontos in Element, dann dort die
Session ``matrix-tldr`` verifizieren). Anfragen anderer Nutzer werden ignoriert,
auch bei ``VERIFY_AUTO_CONFIRM=true``. Die eigentliche Prüfung (Emoji-Vergleich)
findet in Element statt, wo der Mensch bestätigt oder ablehnt.

Fallback, falls dieser Weg bei einer Client-Version nicht funktioniert: manuelle
Verifizierung über den Ed25519-Fingerprint (siehe README).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from nio import (
    AsyncClient,
    KeyVerificationCancel,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationStart,
    LocalProtocolError,
    ToDeviceError,
    ToDeviceEvent,
    UnknownToDeviceEvent,
)
from nio.event_builders import ToDeviceMessage

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

REQUEST_TYPE = "m.key.verification.request"
READY_TYPE = "m.key.verification.ready"
DONE_TYPE = "m.key.verification.done"
CANCEL_TYPE = "m.key.verification.cancel"
SAS_METHOD = "m.sas.v1"
_REQUEST_MAX_AGE_MS = 10 * 60 * 1000

HANDLED_EVENT_TYPES = (
    KeyVerificationStart,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationCancel,
    UnknownToDeviceEvent,
)


def format_emojis(emojis: list[tuple[str, str]]) -> str:
    """Formatiert die SAS-Emojis gut lesbar für das Log (zwei Zeilen wie in Element)."""
    symbols = "   ".join(symbol for symbol, _ in emojis)
    names = " | ".join(name for _, name in emojis)
    return f"\n    {symbols}\n    ({names})"


class VerificationHandler:
    """Verarbeitet To-Device-Ereignisse der Geräteverifizierung."""

    def __init__(
        self,
        client: AsyncClient,
        *,
        auto_confirm: bool,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        """Erzeugt den Handler.

        Args:
            client: Der Matrix-Client.
            auto_confirm: Bestätigt die Emojis auf Bot-Seite automatisch
                (VERIFY_AUTO_CONFIRM). Der Mensch vergleicht in Element.
            clock_ms: Zeitquelle in ms (für Tests).
        """
        self._client = client
        self._auto_confirm = auto_confirm
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._peers: dict[str, tuple[str, str]] = {}  # transaction_id -> (user, device)

    async def on_to_device(self, event: ToDeviceEvent) -> None:
        """Einstiegspunkt für alle relevanten To-Device-Ereignisse. Wirft nie."""
        try:
            if isinstance(event, UnknownToDeviceEvent):
                if event.type == REQUEST_TYPE:
                    await self._on_request(event)
            elif isinstance(event, KeyVerificationStart):
                await self._on_start(event)
            elif isinstance(event, KeyVerificationKey):
                await self._on_key(event)
            elif isinstance(event, KeyVerificationMac):
                await self._on_mac(event)
            elif isinstance(event, KeyVerificationCancel):
                self._on_cancel(event)
        except Exception:
            logger.exception("Fehler in der Geräteverifizierung")

    def _is_own_user(self, sender: str) -> bool:
        return sender == self._client.user_id

    async def _send(self, message_type: str, user: str, device: str, content: dict) -> None:
        message = ToDeviceMessage(message_type, user, device, content)
        response = await self._client.to_device(message)
        if isinstance(response, ToDeviceError):
            logger.warning("Senden von %s fehlgeschlagen: %s", message_type, response.message)

    async def _on_request(self, event: UnknownToDeviceEvent) -> None:
        content = event.source.get("content", {})
        transaction_id = content.get("transaction_id")
        from_device = content.get("from_device")
        methods = content.get("methods") or []
        if not isinstance(transaction_id, str) or not isinstance(from_device, str):
            logger.warning("Verifizierungsanfrage ohne transaction_id/from_device ignoriert")
            return
        if not self._is_own_user(event.sender):
            logger.warning(
                "Verifizierungsanfrage von fremdem Nutzer %s ignoriert "
                "(nur Sessions des Bot-Kontos werden akzeptiert)",
                event.sender,
            )
            return
        timestamp = content.get("timestamp")
        if isinstance(timestamp, int) and abs(self._clock_ms() - timestamp) > _REQUEST_MAX_AGE_MS:
            logger.info("Veraltete Verifizierungsanfrage %s ignoriert", transaction_id)
            return
        if SAS_METHOD not in methods:
            logger.warning("Anfrage %s bietet kein %s an (%s)", transaction_id, SAS_METHOD, methods)
            await self._send(
                CANCEL_TYPE,
                event.sender,
                from_device,
                {
                    "transaction_id": transaction_id,
                    "code": "m.unknown_method",
                    "reason": "Nur m.sas.v1 (Emoji-Vergleich) wird unterstützt",
                },
            )
            return

        # Der anschließende "start" scheitert, wenn die Geräteschlüssel des Gegenübers fehlen.
        if self._client.should_query_keys:
            await self._client.keys_query()

        self._peers[transaction_id] = (event.sender, from_device)
        await self._send(
            READY_TYPE,
            event.sender,
            from_device,
            {
                "from_device": self._client.device_id,
                "methods": [SAS_METHOD],
                "transaction_id": transaction_id,
            },
        )
        logger.info(
            "Verifizierungsanfrage von %s/%s angenommen (%s), warte auf Emoji-Austausch",
            event.sender,
            from_device,
            transaction_id,
        )

    async def _on_start(self, event: KeyVerificationStart) -> None:
        if not self._is_own_user(event.sender):
            logger.warning("Verifizierungsstart von fremdem Nutzer %s abgelehnt", event.sender)
            await self._reject(event)
            return
        if "emoji" not in event.short_authentication_string:
            logger.warning("Gegenstelle unterstützt keine Emoji-Verifizierung, breche ab")
            await self._client.cancel_key_verification(event.transaction_id)
            return
        self._peers.setdefault(event.transaction_id, (event.sender, event.from_device))
        await self._client.accept_key_verification(event.transaction_id)

    async def _reject(self, event: KeyVerificationStart) -> None:
        await self._send(
            CANCEL_TYPE,
            event.sender,
            event.from_device,
            {
                "transaction_id": event.transaction_id,
                "code": "m.user",
                "reason": "Verifizierung nur für das Bot-Konto möglich",
            },
        )

    async def _on_key(self, event: KeyVerificationKey) -> None:
        sas = self._client.key_verifications.get(event.transaction_id)
        if sas is None:
            return
        logger.info(
            "SAS-Emojis der Verifizierung (vergleiche sie mit Element):%s",
            format_emojis(sas.get_emoji()),
        )
        if self._auto_confirm:
            await self._client.confirm_short_auth_string(event.transaction_id)
            logger.info(
                "VERIFY_AUTO_CONFIRM=true: Bot hat bestätigt. Bestätige jetzt in Element, "
                "dass die Emojis übereinstimmen."
            )
        else:
            logger.warning(
                "VERIFY_AUTO_CONFIRM=false: Der Bot bestätigt NICHT automatisch, die "
                "Verifizierung läuft in einen Timeout. Zum Verifizieren: VERIFY_AUTO_CONFIRM=true "
                "setzen, Bot neu starten, Verifizierung in Element erneut starten, Emojis dort "
                "mit diesem Log vergleichen und bestätigen, danach wieder auf false stellen."
            )

    async def _on_mac(self, event: KeyVerificationMac) -> None:
        sas = self._client.key_verifications.get(event.transaction_id)
        if sas is None:
            return
        if sas.canceled:
            logger.warning("Verifizierung %s wurde abgebrochen", event.transaction_id)
            return
        if not sas.verified:
            return  # eigene Bestätigung steht noch aus (bzw. folgt)
        self._client.verify_device(sas.other_olm_device)
        logger.info(
            "Gerät %s/%s ist jetzt verifiziert",
            sas.other_olm_device.user_id,
            sas.other_olm_device.id,
        )
        peer = self._peers.pop(event.transaction_id, None)
        if peer:
            try:
                await self._send(
                    DONE_TYPE, peer[0], peer[1], {"transaction_id": event.transaction_id}
                )
            except LocalProtocolError:
                logger.debug("done konnte nicht gesendet werden", exc_info=True)

    def _on_cancel(self, event: KeyVerificationCancel) -> None:
        self._peers.pop(event.transaction_id, None)
        logger.warning(
            "Verifizierung %s abgebrochen: %s (%s)", event.transaction_id, event.reason, event.code
        )
