"""Matrix-Bot: Client, Session, Event-Filter, Sync-Loop und Link-Verarbeitung."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict, dataclass
from html import escape
from pathlib import Path

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    JoinError,
    LocalProtocolError,
    LoginError,
    MatrixRoom,
    MegolmEvent,
    RoomMessageText,
    RoomSendError,
    SyncError,
)

from .ai.base import Summarizer, SummarizerError
from .config import Settings
from .fetcher import (
    ContentTooLargeError,
    FetchBlockedError,
    FetchConnectionError,
    Fetcher,
    FetchHTTPError,
    FetchTimeoutError,
    NoContentError,
    UnsupportedContentError,
)
from .url_utils import display_domain, extract_urls, strip_reply_fallback
from .verification import HANDLED_EVENT_TYPES, VerificationHandler

logger = logging.getLogger(__name__)

MSG_BLOCKED = "Diesen Link kann ich aus Sicherheitsgründen nicht abrufen."
MSG_UNREACHABLE = "Die Seite war leider nicht erreichbar."
MSG_NO_CONTENT = "Auf dieser Seite konnte ich keinen lesbaren Text finden (evtl. Paywall)."
MSG_UNSUPPORTED = "Diesen Inhaltstyp (z. B. PDF/Video) kann ich nicht zusammenfassen."
MSG_TOO_LARGE = "Die Seite ist zu groß, um sie zusammenzufassen."
MSG_AI_FAILED = "Die Zusammenfassung ist gerade fehlgeschlagen, bitte später erneut versuchen."

MAX_CONCURRENT_FETCHES = 3
DEDUP_CACHE_SIZE = 2048
SYNC_TIMEOUT_MS = 30_000
TYPING_TIMEOUT_MS = 30_000
TYPING_RENEW_SECONDS = 20.0
_SHUTDOWN_GRACE_SECONDS = 10.0


class BotError(Exception):
    """Nicht behebbarer Fehler beim Start (Login, Session, Initial-Sync)."""


# --------------------------------------------------------------------------- Hilfsklassen


class LRUSet:
    """Kleiner LRU-Cache für bereits verarbeitete Event-IDs."""

    def __init__(self, capacity: int) -> None:
        """Legt den Cache mit fester Kapazität an."""
        self._capacity = capacity
        self._items: OrderedDict[str, None] = OrderedDict()

    def add_if_new(self, key: str) -> bool:
        """Merkt ``key`` vor. Gibt ``False`` zurück, wenn er schon bekannt war."""
        if key in self._items:
            self._items.move_to_end(key)
            return False
        self._items[key] = None
        if len(self._items) > self._capacity:
            self._items.popitem(last=False)
        return True


class RateLimiter:
    """Gleitendes Fenster pro Raum (schützt vor hohen KI-Kosten)."""

    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Erzeugt den Limiter: höchstens ``limit`` Aktionen je Fenster und Raum."""
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        """Registriert eine Aktion, sofern das Limit nicht erreicht ist."""
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True


class TypingIndicator:
    """Async-Kontextmanager: Tippt-Anzeige setzen, erneuern und IMMER zurücksetzen."""

    def __init__(
        self,
        client: AsyncClient,
        room_id: str,
        *,
        renew_seconds: float = TYPING_RENEW_SECONDS,
        timeout_ms: int = TYPING_TIMEOUT_MS,
    ) -> None:
        """Erzeugt den Indicator für einen Raum."""
        self._client = client
        self._room_id = room_id
        self._renew_seconds = renew_seconds
        self._timeout_ms = timeout_ms
        self._task: asyncio.Task[None] | None = None

    async def _set(self, typing: bool) -> None:
        try:
            await self._client.room_typing(self._room_id, typing, timeout=self._timeout_ms)
        except Exception:
            logger.debug("room_typing(%s) fehlgeschlagen", typing, exc_info=True)

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(self._renew_seconds)
            await self._set(True)

    async def __aenter__(self) -> TypingIndicator:
        await self._set(True)
        self._task = asyncio.create_task(self._renew())
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._set(False)


# --------------------------------------------------------------------------- reine Funktionen


def reject_reason(
    *,
    sender: str,
    own_user_id: str,
    content: Mapping[str, object],
    server_timestamp_ms: int,
    start_time_ms: int,
    room_ids: Collection[str | None],
    allowed_rooms: Collection[str],
) -> str | None:
    """Entscheidet, ob ein Nachrichten-Event verworfen wird.

    Returns:
        Grund für das Verwerfen oder ``None``, wenn das Event verarbeitet wird.
    """
    if sender == own_user_id:
        return "eigene Nachricht"
    if content.get("msgtype") == "m.notice":
        return "m.notice (Bot-Schleifen-Schutz)"
    relates_to = content.get("m.relates_to")
    if isinstance(relates_to, Mapping) and relates_to.get("rel_type") == "m.replace":
        return "Edit"
    if server_timestamp_ms < start_time_ms:
        return "älter als die Startzeit"
    if allowed_rooms and not any(room_id in allowed_rooms for room_id in room_ids if room_id):
        return "Raum nicht in ALLOWED_ROOMS"
    return None


def _reply_relation(event_id: str) -> dict[str, object]:
    return {"m.in_reply_to": {"event_id": event_id}}


def build_summary_reply(summary: str, domain: str, reply_to_event_id: str) -> dict[str, object]:
    """Baut die Matrix-Antwort ``🔗 <Domain>: <Zusammenfassung>`` (m.notice, echter Reply)."""
    return {
        "msgtype": "m.notice",
        "body": f"🔗 {domain}: {summary}",
        "format": "org.matrix.custom.html",
        "formatted_body": f"🔗 <b>{escape(domain)}</b>: {escape(summary)}",
        "m.relates_to": _reply_relation(reply_to_event_id),
    }


def build_text_reply(text: str, reply_to_event_id: str) -> dict[str, object]:
    """Baut eine kurze Fehler-/Hinweisantwort als m.notice-Reply."""
    return {
        "msgtype": "m.notice",
        "body": text,
        "format": "org.matrix.custom.html",
        "formatted_body": escape(text),
        "m.relates_to": _reply_relation(reply_to_event_id),
    }


def format_fingerprint(key: str) -> str:
    """Gruppiert den Ed25519-Fingerprint in Viererblöcke wie Element ihn anzeigt."""
    return " ".join(key[i : i + 4] for i in range(0, len(key), 4))


# --------------------------------------------------------------------------- Session


@dataclass(frozen=True, slots=True)
class StoredSession:
    """Persistente Anmeldedaten. Verhindert neue Geräte bei jedem Start."""

    homeserver: str
    user_id: str
    device_id: str
    access_token: str
    pickle_key: str


def load_session(path: Path) -> StoredSession | None:
    """Liest die Session-Datei. ``None``, wenn sie fehlt."""
    if not path.exists():
        return None
    try:
        return StoredSession(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        raise BotError(
            f"Session-Datei {path} ist unlesbar ({exc}). Löschen und mit MATRIX_PASSWORD "
            "neu anmelden (achtung: das erzeugt ein neues Gerät, Verifizierung wiederholen)."
        ) from exc


def save_session(path: Path, session: StoredSession) -> None:
    """Speichert die Session atomar mit Dateirechten 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(asdict(session), handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- Bot


class TldrBot:
    """Überwacht Räume und antwortet auf https-Links mit Zwei-Satz-Zusammenfassungen."""

    def __init__(self, settings: Settings, summarizer: Summarizer, fetcher: Fetcher) -> None:
        """Erzeugt den Bot (noch ohne Netzwerkzugriff)."""
        self._settings = settings
        self._summarizer = summarizer
        self._fetcher = fetcher
        self._client: AsyncClient | None = None
        self._verification: VerificationHandler | None = None
        self._seen = LRUSet(DEDUP_CACHE_SIZE)
        self._limiter = RateLimiter(settings.rate_limit_per_room_per_minute)
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._start_time_ms = 0
        self._fatal: str | None = None

    # ----- Lebenszyklus

    def request_stop(self) -> None:
        """Fordert ein sauberes Herunterfahren an (z. B. aus einem Signal-Handler)."""
        self._stop.set()

    async def run(self) -> None:
        """Startet den Bot und läuft bis zum Stop. Räumt immer auf.

        Raises:
            BotError: Bei nicht behebbaren Startfehlern oder ungültigem Token.
        """
        try:
            await self._startup()
            sync_task = asyncio.create_task(self._sync_supervisor(), name="sync")
            stop_task = asyncio.create_task(self._stop.wait(), name="stop-wait")
            await asyncio.wait({sync_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            stop_task.cancel()
            await self._shutdown(sync_task)
        finally:
            await self._close_resources()
        if self._fatal:
            raise BotError(self._fatal)

    async def _startup(self) -> None:
        self._settings.matrix_store_path.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self._settings.matrix_store_path, 0o700)

        client = await self._login()
        self._client = client
        self._log_identity(client)

        self._verification = VerificationHandler(
            client, auto_confirm=self._settings.verify_auto_confirm
        )
        # Einladungen und Verifizierung VOR dem Initial-Sync registrieren: nio ruft
        # Callbacks schon innerhalb von sync() auf, sonst gingen Einladungen und
        # Verifizierungsanfragen aus der Offline-Zeit verloren.
        client.add_event_callback(self.on_invite, InviteMemberEvent)
        client.add_to_device_callback(self._verification.on_to_device, HANDLED_EVENT_TYPES)
        client.add_response_callback(self._on_sync_error, SyncError)

        # Startzeit vor dem Initial-Sync merken (doppelte Absicherung zum Initial-Sync).
        self._start_time_ms = int(time.time() * 1000)
        await self._initial_sync(client)

        # Erst jetzt die Nachrichten-Callbacks, damit der Verlauf nicht verarbeitet wird.
        client.add_event_callback(self.on_message, RoomMessageText)
        client.add_event_callback(self.on_undecryptable, MegolmEvent)

        for room_id, room in list(client.invited_rooms.items()):
            await self._maybe_join(room_id, getattr(room, "inviter", None))

        logger.info(
            "Bot läuft. Überwachte Räume: %d, erlaubte Räume: %s",
            len(client.rooms),
            sorted(self._settings.allowed_rooms) or "alle",
        )

    def _new_client(self, device_id: str, pickle_key: str) -> AsyncClient:
        config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=True,
            pickle_key=pickle_key,
        )
        return AsyncClient(
            self._settings.matrix_homeserver,
            self._settings.matrix_user_id,
            device_id=device_id,
            store_path=str(self._settings.matrix_store_path),
            config=config,
        )

    async def _login(self) -> AsyncClient:
        settings = self._settings
        stored = load_session(settings.session_path)
        if stored is not None:
            if (stored.user_id, stored.homeserver) != (
                settings.matrix_user_id,
                settings.matrix_homeserver,
            ):
                raise BotError(
                    f"{settings.session_path} gehört zu {stored.user_id} auf {stored.homeserver}, "
                    "konfiguriert ist ein anderes Konto. Store-Pfad oder Konfiguration prüfen."
                )
            client = self._new_client(stored.device_id, stored.pickle_key)
            client.restore_login(stored.user_id, stored.device_id, stored.access_token)
            logger.info("Session wiederhergestellt (Gerät %s)", stored.device_id)
            return client

        if not settings.matrix_password:
            raise BotError("Keine gespeicherte Session und kein MATRIX_PASSWORD gesetzt")
        pickle_key = secrets.token_urlsafe(32)
        client = self._new_client("", pickle_key)
        response = await client.login(
            settings.matrix_password, device_name=settings.matrix_device_name
        )
        if isinstance(response, LoginError):
            await client.close()
            raise BotError(f"Login fehlgeschlagen: {response.message}")
        save_session(
            settings.session_path,
            StoredSession(
                homeserver=settings.matrix_homeserver,
                user_id=response.user_id,
                device_id=response.device_id,
                access_token=response.access_token,
                pickle_key=pickle_key,
            ),
        )
        logger.info(
            "Erster Login erfolgreich, Session gespeichert (Gerät %s). "
            "MATRIX_PASSWORD wird künftig nicht mehr benötigt.",
            response.device_id,
        )
        return client

    def _log_identity(self, client: AsyncClient) -> None:
        logger.info("Geräte-ID: %s", client.device_id)
        try:
            fingerprint = client.olm.account.identity_keys["ed25519"]  # type: ignore[union-attr]
        except Exception:
            logger.warning("Ed25519-Fingerprint nicht verfügbar (E2EE inaktiv?)", exc_info=True)
            return
        logger.info("Ed25519-Fingerprint: %s", format_fingerprint(fingerprint))

    async def _initial_sync(self, client: AsyncClient) -> None:
        last_error = "unbekannt"
        for attempt in range(1, 6):
            response = await client.sync(timeout=SYNC_TIMEOUT_MS, full_state=True)
            if not isinstance(response, SyncError):
                break
            last_error = f"{response.status_code}: {response.message}"
            if response.status_code == "M_UNKNOWN_TOKEN":
                raise BotError(self._token_hint())
            logger.warning("Initial-Sync fehlgeschlagen (%d/5): %s", attempt, last_error)
            await asyncio.sleep(2 * attempt)
        else:
            raise BotError(f"Initial-Sync fehlgeschlagen: {last_error}")

        # Schlüssel nach dem Initial-Sync bereitstellen; sync_forever übernimmt danach.
        if client.should_upload_keys:
            await client.keys_upload()
        if client.should_query_keys:
            await client.keys_query()

    def _token_hint(self) -> str:
        return (
            "Der Access-Token ist ungültig (Session beendet?). "
            f"{self._settings.session_path} löschen und mit MATRIX_PASSWORD neu anmelden; "
            "das erzeugt ein neues Gerät, die Verifizierung muss wiederholt werden."
        )

    async def _on_sync_error(self, response: SyncError) -> None:
        if response.status_code == "M_UNKNOWN_TOKEN":
            logger.critical("%s", self._token_hint())
            self._fatal = self._token_hint()
            self.request_stop()
        else:
            logger.warning("Sync-Fehler: %s %s", response.status_code, response.message)

    async def _sync_supervisor(self) -> None:
        """Hält den Sync-Loop am Leben: Fehler werden geloggt, nicht weitergereicht."""
        assert self._client is not None
        backoff = 5.0
        while not self._stop.is_set():
            try:
                await self._client.sync_forever(timeout=SYNC_TIMEOUT_MS, full_state=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Sync-Loop abgestürzt, Neustart in %.0f s", backoff)
            else:
                backoff = 5.0
            if self._stop.is_set():
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, 60.0)

    async def _shutdown(self, sync_task: asyncio.Task[None]) -> None:
        logger.info("Fahre herunter ...")
        assert self._client is not None
        self._client.stop_sync_forever()
        try:
            await asyncio.wait_for(asyncio.shield(sync_task), timeout=_SHUTDOWN_GRACE_SECONDS)
        except (TimeoutError, asyncio.CancelledError):
            sync_task.cancel()
        except Exception:
            logger.exception("Sync-Task endete mit Fehler")
        await asyncio.gather(sync_task, return_exceptions=True)

        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _close_resources(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
        for closer in (self._fetcher.aclose, self._summarizer.aclose):
            try:
                await closer()
            except Exception:
                logger.exception("Fehler beim Schließen von Ressourcen")

    # ----- Callbacks

    async def on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        """Nimmt Einladungen an (optional nur von AUTO_JOIN_ALLOWED_INVITERS)."""
        try:
            assert self._client is not None
            if event.membership != "invite" or event.state_key != self._client.user_id:
                return
            await self._maybe_join(room.room_id, event.sender)
        except Exception:
            logger.exception("Fehler beim Verarbeiten einer Einladung (room=%s)", room.room_id)

    async def _maybe_join(self, room_id: str, inviter: str | None) -> None:
        assert self._client is not None
        allowed = self._settings.auto_join_allowed_inviters
        if allowed and inviter not in allowed:
            logger.warning(
                "Einladung in %s von nicht erlaubtem Nutzer %s ignoriert", room_id, inviter
            )
            return
        for attempt in range(1, 4):
            response = await self._client.join(room_id)
            if not isinstance(response, JoinError):
                logger.info("Raum %s beigetreten (eingeladen von %s)", room_id, inviter)
                return
            logger.warning(
                "Beitritt zu %s fehlgeschlagen (%d/3): %s", room_id, attempt, response.message
            )
            await asyncio.sleep(2 * attempt)

    async def on_undecryptable(self, room: MatrixRoom, event: MegolmEvent) -> None:
        """Nicht entschlüsselbare Nachricht: warnen und Schlüssel anfordern, nie abstürzen."""
        logger.warning(
            "Nicht entschlüsselbar (room=%s event=%s session=%s), fordere Schlüssel an",
            room.room_id,
            event.event_id,
            event.session_id,
        )
        try:
            assert self._client is not None
            await self._client.request_room_key(event)
        except LocalProtocolError:
            logger.debug("Schlüsselanfrage für %s bereits gestellt", event.session_id)
        except Exception:
            logger.exception("Schlüsselanfrage fehlgeschlagen (room=%s)", room.room_id)

    async def on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Filtert Nachrichten und startet für Links einen eigenen Task."""
        try:
            self._handle_message(room, event)
        except Exception:
            logger.exception(
                "Fehler beim Filtern eines Events (room=%s event=%s)", room.room_id, event.event_id
            )

    def _handle_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        assert self._client is not None
        content = event.source.get("content", {})
        reason = reject_reason(
            sender=event.sender,
            own_user_id=self._client.user_id,
            content=content,
            server_timestamp_ms=event.server_timestamp,
            start_time_ms=self._start_time_ms,
            # Nur die Raum-ID: canonical_alias ist von Raum-Admins frei setzbarer State
            # und darf nie zur Autorisierung dienen.
            room_ids=(room.room_id,),
            allowed_rooms=self._settings.allowed_rooms,
        )
        if reason:
            logger.debug("Event %s verworfen: %s", event.event_id, reason)
            return
        if not self._seen.add_if_new(event.event_id):
            logger.debug("Event %s bereits verarbeitet", event.event_id)
            return

        body, formatted = event.body, event.formatted_body
        relates_to = content.get("m.relates_to")
        if isinstance(relates_to, Mapping) and "m.in_reply_to" in relates_to:
            body, formatted = strip_reply_fallback(body, formatted)
        urls = extract_urls(body, formatted, max_links=self._settings.max_links_per_message)
        if not urls:
            return

        task = asyncio.create_task(
            self._process_urls(room.room_id, event.event_id, urls), name=f"tldr:{event.event_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Task %s mit Fehler beendet", task.get_name(), exc_info=exc)

    # ----- Link-Verarbeitung

    async def _process_urls(self, room_id: str, event_id: str, urls: list[str]) -> None:
        for url in urls:
            await self._process_url(room_id, event_id, url)

    async def _process_url(self, room_id: str, event_id: str, url: str) -> None:
        assert self._client is not None
        domain = display_domain(url)
        if not self._limiter.allow(room_id):
            logger.warning(
                "Rate-Limit erreicht (room=%s event=%s domain=%s)", room_id, event_id, domain
            )
            return

        async with TypingIndicator(self._client, room_id):
            content = await self._build_reply(room_id, event_id, url, domain)
            await self._send(room_id, content)

    async def _build_reply(
        self, room_id: str, event_id: str, url: str, domain: str
    ) -> dict[str, object]:
        """Ruft ab, fasst zusammen und liefert den Antwortinhalt. Wirft nie (außer Cancel)."""
        ctx = f"room={room_id} event={event_id} domain={domain}"
        try:
            async with self._semaphore:
                page = await self._fetcher.fetch(url)
            summary = await self._summarizer.summarize(page.text, page.title, page.url)
        except FetchBlockedError as exc:
            logger.warning("SSRF-Schutz hat Link blockiert (%s): %s", ctx, exc)
            return build_text_reply(MSG_BLOCKED, event_id)
        except (FetchTimeoutError, FetchConnectionError, FetchHTTPError) as exc:
            logger.warning("Seite nicht erreichbar (%s): %s", ctx, exc)
            return build_text_reply(MSG_UNREACHABLE, event_id)
        except NoContentError as exc:
            logger.info("Kein lesbarer Inhalt (%s): %s", ctx, exc)
            return build_text_reply(MSG_NO_CONTENT, event_id)
        except UnsupportedContentError as exc:
            logger.info("Nicht unterstützter Inhaltstyp (%s): %s", ctx, exc)
            return build_text_reply(MSG_UNSUPPORTED, event_id)
        except ContentTooLargeError as exc:
            logger.info("Seite zu groß (%s): %s", ctx, exc)
            return build_text_reply(MSG_TOO_LARGE, event_id)
        except SummarizerError as exc:
            logger.error("KI-Fehler (%s): %s", ctx, exc)
            return build_text_reply(MSG_AI_FAILED, event_id)
        except Exception:
            logger.exception("Unerwarteter Fehler (%s)", ctx)
            return build_text_reply(MSG_AI_FAILED, event_id)
        logger.info("Zusammenfassung erstellt (%s)", ctx)
        return build_summary_reply(summary, domain, event_id)

    async def _send(self, room_id: str, content: dict[str, object]) -> None:
        assert self._client is not None
        try:
            response = await self._client.room_send(
                room_id,
                "m.room.message",
                content,
                ignore_unverified_devices=True,  # sonst schlägt room_send in E2EE-Räumen fehl
            )
        except Exception:
            logger.exception("Senden fehlgeschlagen (room=%s)", room_id)
            return
        if isinstance(response, RoomSendError):
            logger.error("Senden abgelehnt (room=%s): %s", room_id, response.message)
