"""Konfiguration: lädt die ``.env``, validiert alle Werte und liefert ``Settings``.

Beim Start werden alle Pflichtwerte geprüft. Fehler werden gesammelt und in
einer einzigen, klaren ``ConfigError``-Meldung ausgegeben, damit der Betreiber
nicht Fehler für Fehler nachbessern muss.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

SESSION_FILENAME = "session.json"
SUPPORTED_PROVIDERS = ("openai", "anthropic")


class ConfigError(Exception):
    """Ungültige oder fehlende Konfiguration."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Unveränderliche, validierte Konfiguration des Bots."""

    matrix_homeserver: str
    matrix_user_id: str
    matrix_password: str | None
    matrix_device_name: str
    matrix_store_path: Path

    ai_provider: str
    ai_model: str
    openai_api_key: str | None
    openai_base_url: str | None
    anthropic_api_key: str | None
    ai_max_input_chars: int
    ai_timeout_seconds: float
    ai_effort: str | None

    fetch_timeout_seconds: float
    fetch_max_bytes: int

    max_links_per_message: int
    rate_limit_per_room_per_minute: int
    allowed_rooms: frozenset[str]
    auto_join_allowed_inviters: frozenset[str]
    verify_auto_confirm: bool
    log_level: str

    @property
    def session_path(self) -> Path:
        """Pfad der persistenten Session-Datei im Store-Verzeichnis."""
        return self.matrix_store_path / SESSION_FILENAME


def _parse_bool(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    return None


def _parse_list(raw: str) -> frozenset[str]:
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def load_settings(env: Mapping[str, str] | None = None, *, read_dotenv: bool = True) -> Settings:
    """Lädt und validiert die Konfiguration.

    Args:
        env: Quelle der Werte. ``None`` bedeutet ``os.environ`` (nach dem Laden
            einer ``.env``-Datei, sofern ``read_dotenv`` gesetzt ist).
        read_dotenv: Ob eine vorhandene ``.env`` geladen werden soll.

    Raises:
        ConfigError: Wenn Pflichtwerte fehlen oder Werte ungültig sind. Die
            Meldung listet alle gefundenen Probleme auf.
    """
    if env is None:
        if read_dotenv:
            load_dotenv(override=False)
        env = os.environ

    errors: list[str] = []

    def get(name: str, default: str | None = None) -> str | None:
        value = env.get(name)
        if value is None or not value.strip():
            return default
        return value.strip()

    def required(name: str, hint: str) -> str:
        value = get(name)
        if value is None:
            errors.append(f"{name} fehlt ({hint})")
            return ""
        return value

    def number(name: str, default: str, *, minimum: float, integer: bool) -> float:
        raw = get(name, default) or default
        try:
            value = int(raw) if integer else float(raw)
        except ValueError:
            errors.append(f"{name}='{raw}' ist keine gültige Zahl")
            return float(default)
        if value < minimum:
            errors.append(f"{name}={value} ist zu klein (Minimum: {minimum:g})")
        return value

    homeserver = required("MATRIX_HOMESERVER", "z. B. https://matrix.example.org")
    if homeserver and not homeserver.lower().startswith(("https://", "http://")):
        errors.append("MATRIX_HOMESERVER muss mit https:// (oder http://) beginnen")
    homeserver = homeserver.rstrip("/")

    user_id = required("MATRIX_USER_ID", "z. B. @tldr:example.org")
    if user_id and not (user_id.startswith("@") and ":" in user_id[2:]):
        errors.append("MATRIX_USER_ID muss die Form @name:server haben")

    store_path = Path(get("MATRIX_STORE_PATH", "./store") or "./store")
    password = get("MATRIX_PASSWORD")
    if password is None and not (store_path / SESSION_FILENAME).exists():
        errors.append(
            "MATRIX_PASSWORD fehlt und es gibt noch keine gespeicherte Session "
            f"({store_path / SESSION_FILENAME}); das Passwort wird für den ersten "
            "Login benötigt"
        )

    provider = (get("AI_PROVIDER", "anthropic") or "anthropic").lower()
    if provider not in SUPPORTED_PROVIDERS:
        errors.append(
            f"AI_PROVIDER='{provider}' ist unbekannt (erlaubt: {', '.join(SUPPORTED_PROVIDERS)})"
        )

    model = required("AI_MODEL", "Modellname des gewählten Providers")
    openai_key = get("OPENAI_API_KEY")
    anthropic_key = get("ANTHROPIC_API_KEY")
    if provider == "openai" and not openai_key:
        errors.append("OPENAI_API_KEY fehlt (AI_PROVIDER=openai)")
    if provider == "anthropic" and not anthropic_key:
        errors.append("ANTHROPIC_API_KEY fehlt (AI_PROVIDER=anthropic)")

    base_url = get("OPENAI_BASE_URL")
    if base_url and not base_url.lower().startswith(("https://", "http://")):
        errors.append("OPENAI_BASE_URL muss mit https:// (oder http://) beginnen")

    effort = (get("AI_EFFORT", "low") or "").lower()
    if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        errors.append("AI_EFFORT muss none, low, medium, high, xhigh oder max sein")

    verify_raw = get("VERIFY_AUTO_CONFIRM", "false") or "false"
    verify_auto = _parse_bool(verify_raw)
    if verify_auto is None:
        errors.append(f"VERIFY_AUTO_CONFIRM='{verify_raw}' ist weder true noch false")
        verify_auto = False

    log_level = (get("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        errors.append(f"LOG_LEVEL='{log_level}' ist ungültig")
        log_level = "INFO"

    max_input = int(number("AI_MAX_INPUT_CHARS", "12000", minimum=500, integer=True))
    ai_timeout = float(number("AI_TIMEOUT_SECONDS", "30", minimum=1, integer=False))
    fetch_timeout = float(number("FETCH_TIMEOUT_SECONDS", "10", minimum=1, integer=False))
    fetch_max = int(number("FETCH_MAX_BYTES", "5242880", minimum=1024, integer=True))
    max_links = int(number("MAX_LINKS_PER_MESSAGE", "1", minimum=1, integer=True))
    rate_limit = int(number("RATE_LIMIT_PER_ROOM_PER_MINUTE", "5", minimum=1, integer=True))

    if errors:
        details = "\n  - ".join(errors)
        raise ConfigError(f"Ungültige Konfiguration:\n  - {details}")

    return Settings(
        matrix_homeserver=homeserver,
        matrix_user_id=user_id,
        matrix_password=password,
        matrix_device_name=get("MATRIX_DEVICE_NAME", "matrix-tldr") or "matrix-tldr",
        matrix_store_path=store_path,
        ai_provider=provider,
        ai_model=model,
        openai_api_key=openai_key,
        openai_base_url=base_url,
        anthropic_api_key=anthropic_key,
        ai_max_input_chars=max_input,
        ai_timeout_seconds=ai_timeout,
        ai_effort=None if effort == "none" else effort,
        fetch_timeout_seconds=fetch_timeout,
        fetch_max_bytes=fetch_max,
        max_links_per_message=max_links,
        rate_limit_per_room_per_minute=rate_limit,
        allowed_rooms=_parse_list(get("ALLOWED_ROOMS", "") or ""),
        auto_join_allowed_inviters=_parse_list(get("AUTO_JOIN_ALLOWED_INVITERS", "") or ""),
        verify_auto_confirm=verify_auto,
        log_level=log_level,
    )
