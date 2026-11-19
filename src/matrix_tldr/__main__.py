"""Einstiegspunkt: ``python -m matrix_tldr``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .ai import get_summarizer
from .bot import BotError, TldrBot
from .config import ConfigError, Settings, load_settings
from .fetcher import Fetcher

logger = logging.getLogger("matrix_tldr")


def configure_logging(level: str) -> None:
    """Richtet Logging ein. HTTP-Bibliotheken bleiben leise, sie loggen volle URLs."""
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    quiet = logging.DEBUG if level == "DEBUG" else logging.WARNING
    for name in ("httpx", "httpcore", "anthropic", "openai", "aiohttp"):
        logging.getLogger(name).setLevel(quiet)
    logging.getLogger("nio").setLevel(max(getattr(logging, level), logging.INFO))


async def _run(settings: Settings) -> None:
    fetcher = Fetcher(
        timeout_seconds=settings.fetch_timeout_seconds, max_bytes=settings.fetch_max_bytes
    )
    summarizer = get_summarizer(settings)
    bot = TldrBot(settings, summarizer, fetcher)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bot.request_stop)

    logger.info(
        "Starte matrix-tldr (Provider %s, Modell %s)", settings.ai_provider, settings.ai_model
    )
    await bot.run()


def main() -> int:
    """Lädt die Konfiguration, startet den Bot und gibt den Exit-Code zurück."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    try:
        asyncio.run(_run(settings))
    except BotError as exc:
        logger.critical("%s", exc)
        return 1
    except KeyboardInterrupt:
        pass
    logger.info("Beendet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
