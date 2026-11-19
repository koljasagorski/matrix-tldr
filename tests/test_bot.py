import asyncio
import json
import os
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from nio import MatrixRoom, RoomMessageText, RoomSendError, UnknownToDeviceEvent

from matrix_tldr.ai.base import SummarizerError
from matrix_tldr.bot import (
    MSG_AI_FAILED,
    MSG_BLOCKED,
    MSG_NO_CONTENT,
    MSG_UNREACHABLE,
    MSG_UNSUPPORTED,
    LRUSet,
    RateLimiter,
    StoredSession,
    TldrBot,
    TypingIndicator,
    build_summary_reply,
    build_text_reply,
    format_fingerprint,
    load_session,
    reject_reason,
    save_session,
)
from matrix_tldr.config import load_settings
from matrix_tldr.fetcher import (
    FetchBlockedError,
    FetchResult,
    FetchTimeoutError,
    NoContentError,
    UnsupportedContentError,
)
from matrix_tldr.verification import VerificationHandler

BOT = "@bot:example.org"
START = 1_000_000


def filter_args(**overrides):
    args = {
        "sender": "@user:example.org",
        "own_user_id": BOT,
        "content": {"msgtype": "m.text", "body": "https://example.org"},
        "server_timestamp_ms": START + 10,
        "start_time_ms": START,
        "room_ids": ("!room:example.org", None),
        "allowed_rooms": frozenset(),
    }
    args.update(overrides)
    return args


# ----------------------------------------------------------------- Event-Filter


def test_regular_message_passes():
    assert reject_reason(**filter_args()) is None


def test_own_message_is_dropped():
    assert reject_reason(**filter_args(sender=BOT)) is not None


def test_notice_is_dropped():
    assert reject_reason(**filter_args(content={"msgtype": "m.notice", "body": "x"})) is not None


def test_edit_is_dropped():
    content = {
        "msgtype": "m.text",
        "body": "* neu",
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$alt"},
    }
    assert reject_reason(**filter_args(content=content)) == "Edit"


def test_reply_is_not_an_edit():
    content = {
        "msgtype": "m.text",
        "body": "x",
        "m.relates_to": {"m.in_reply_to": {"event_id": "$a"}},
    }
    assert reject_reason(**filter_args(content=content)) is None


def test_event_older_than_start_time_is_dropped():
    assert reject_reason(**filter_args(server_timestamp_ms=START - 1)) is not None
    assert reject_reason(**filter_args(server_timestamp_ms=START)) is None


def test_room_filter():
    allowed = frozenset({"!erlaubt:example.org"})
    assert reject_reason(**filter_args(allowed_rooms=allowed)) is not None
    assert (
        reject_reason(**filter_args(allowed_rooms=allowed, room_ids=("!erlaubt:example.org",)))
        is None
    )
    alias = frozenset({"#raum:example.org"})
    assert (
        reject_reason(**filter_args(allowed_rooms=alias, room_ids=("!x:y", "#raum:example.org")))
        is None
    )


# ----------------------------------------------------------------- Hilfsklassen


def test_lru_set_deduplicates_and_evicts():
    lru = LRUSet(2)
    assert lru.add_if_new("a") and lru.add_if_new("b")
    assert not lru.add_if_new("a")
    assert lru.add_if_new("c")  # verdrängt "b" (am längsten nicht benutzt)
    assert lru.add_if_new("b")
    assert not lru.add_if_new("c")


def test_rate_limiter_sliding_window():
    now = [0.0]
    limiter = RateLimiter(2, window_seconds=60, clock=lambda: now[0])
    assert limiter.allow("r") and limiter.allow("r")
    assert not limiter.allow("r")
    assert limiter.allow("anderer-raum")
    now[0] = 61
    assert limiter.allow("r")


def test_summary_reply_is_notice_reply_with_escaped_html():
    content = build_summary_reply("a < b & <script>", "example.org", "$ev")
    assert content["msgtype"] == "m.notice"
    assert content["body"] == "🔗 example.org: a < b & <script>"
    assert content["format"] == "org.matrix.custom.html"
    assert (
        "&lt;script&gt;" in content["formatted_body"]
        and "<script>" not in content["formatted_body"]
    )
    assert content["m.relates_to"] == {"m.in_reply_to": {"event_id": "$ev"}}


def test_text_reply_is_notice_reply():
    content = build_text_reply("Hallo", "$ev")
    assert content["msgtype"] == "m.notice" and content["body"] == "Hallo"
    assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$ev"


def test_fingerprint_is_grouped():
    assert format_fingerprint("abcdefghijkl") == "abcd efgh ijkl"


def test_session_roundtrip_with_0600(tmp_path):
    path = tmp_path / "store" / "session.json"
    session = StoredSession("https://h", "@b:h", "DEVICE", "tok", "pk")
    save_session(path, session)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_session(path) == session
    assert json.loads(path.read_text())["device_id"] == "DEVICE"
    assert load_session(tmp_path / "gibt-es-nicht.json") is None


async def test_typing_indicator_sets_renews_and_always_resets():
    client = SimpleNamespace(room_typing=AsyncMock())
    with pytest.raises(RuntimeError):
        async with TypingIndicator(client, "!r:x", renew_seconds=0.01):
            await asyncio.sleep(0.05)
            raise RuntimeError("Fehler im Block")
    calls = [c.args[1] for c in client.room_typing.await_args_list]
    assert calls[0] is True and calls[-1] is False
    assert calls.count(True) >= 2  # wurde erneuert
    assert client.room_typing.await_args_list[0].kwargs["timeout"] == 30000


async def test_typing_indicator_survives_network_errors():
    client = SimpleNamespace(room_typing=AsyncMock(side_effect=OSError("down")))
    async with TypingIndicator(client, "!r:x"):
        pass


# ----------------------------------------------------------------- Bot-Ablauf


def make_bot(fetch=None, summarize=None, *, env_extra=None, now_ms=START):
    env = {
        "MATRIX_HOMESERVER": "https://matrix.example.org",
        "MATRIX_USER_ID": BOT,
        "MATRIX_PASSWORD": "pw",
        "AI_MODEL": "m",
        "ANTHROPIC_API_KEY": "k",
        **(env_extra or {}),
    }
    settings = load_settings(env, read_dotenv=False)
    fetcher = SimpleNamespace(fetch=fetch or AsyncMock(), aclose=AsyncMock())
    summarizer = SimpleNamespace(summarize=summarize or AsyncMock(), aclose=AsyncMock())
    bot = TldrBot(settings, summarizer, fetcher)
    client = SimpleNamespace(
        user_id=BOT,
        room_typing=AsyncMock(),
        room_send=AsyncMock(return_value=SimpleNamespace()),
    )
    bot._client = client
    bot._start_time_ms = now_ms
    return bot, client


def make_event(body, *, event_id="$e1", sender="@user:example.org", ts=START + 5, extra=None):
    content = {"msgtype": "m.text", "body": body, **(extra or {})}
    return RoomMessageText.from_dict(
        {
            "type": "m.room.message",
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": ts,
            "content": content,
        }
    )


ROOM = MatrixRoom("!room:example.org", BOT)
PAGE = FetchResult(url="https://example.org/a", title="T", text="x" * 400)


async def run_message(bot, event):
    await bot.on_message(ROOM, event)
    await asyncio.gather(*bot._tasks)


def sent_content(client):
    return client.room_send.await_args.args[2]


async def test_link_is_summarized_and_posted_as_reply():
    summarize = AsyncMock(return_value="Eins. Zwei.")
    bot, client = make_bot(fetch=AsyncMock(return_value=PAGE), summarize=summarize)
    await run_message(bot, make_event("schaut mal https://www.example.org/a."))

    summarize.assert_awaited_once_with(PAGE.text, "T", PAGE.url)
    args = client.room_send.await_args
    assert args.args[:2] == ("!room:example.org", "m.room.message")
    assert args.kwargs["ignore_unverified_devices"] is True
    content = sent_content(client)
    assert content["body"] == "🔗 example.org: Eins. Zwei."
    assert content["msgtype"] == "m.notice"
    assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$e1"
    typing = [c.args[1] for c in client.room_typing.await_args_list]
    assert typing[0] is True and typing[-1] is False


async def test_message_without_link_is_ignored():
    fetch = AsyncMock()
    bot, client = make_bot(fetch=fetch)
    await run_message(bot, make_event("hallo zusammen, http://example.org zählt nicht"))
    fetch.assert_not_awaited()
    client.room_send.assert_not_awaited()


@pytest.mark.parametrize(
    "event",
    [
        make_event("https://example.org/a", sender=BOT),
        make_event("https://example.org/a", extra={"msgtype": "m.notice"}),
        make_event(
            "https://example.org/a",
            extra={"m.relates_to": {"rel_type": "m.replace", "event_id": "$alt"}},
        ),
        make_event("https://example.org/a", ts=START - 500),
    ],
    ids=["eigene", "notice", "edit", "alt"],
)
async def test_filtered_events_trigger_nothing(event):
    fetch = AsyncMock(return_value=PAGE)
    bot, client = make_bot(fetch=fetch, summarize=AsyncMock(return_value="x"))
    await run_message(bot, event)
    fetch.assert_not_awaited()
    client.room_send.assert_not_awaited()


async def test_same_event_is_processed_only_once():
    fetch = AsyncMock(return_value=PAGE)
    bot, client = make_bot(fetch=fetch, summarize=AsyncMock(return_value="Satz."))
    event = make_event("https://example.org/a")
    await run_message(bot, event)
    await run_message(bot, event)
    assert fetch.await_count == 1 and client.room_send.await_count == 1


async def test_room_filter_blocks_other_rooms():
    fetch = AsyncMock(return_value=PAGE)
    bot, _ = make_bot(fetch=fetch, env_extra={"ALLOWED_ROOMS": "!anderer:example.org"})
    await run_message(bot, make_event("https://example.org/a"))
    fetch.assert_not_awaited()


async def test_only_max_links_are_processed():
    fetch = AsyncMock(return_value=PAGE)
    bot, _client = make_bot(fetch=fetch, summarize=AsyncMock(return_value="Satz."))
    await run_message(
        bot, make_event("https://a.example/1 https://b.example/2 https://c.example/3")
    )
    assert fetch.await_count == 1


async def test_rate_limit_stays_silent_but_does_not_crash():
    bot, client = make_bot(
        fetch=AsyncMock(return_value=PAGE),
        summarize=AsyncMock(return_value="Satz."),
        env_extra={"RATE_LIMIT_PER_ROOM_PER_MINUTE": "2"},
    )
    for i in range(4):
        await run_message(bot, make_event("https://example.org/a", event_id=f"$e{i}"))
    assert client.room_send.await_count == 2  # Rest: nur Log, keine Antwort


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FetchBlockedError("intern"), MSG_BLOCKED),
        (FetchTimeoutError("t"), MSG_UNREACHABLE),
        (NoContentError("leer"), MSG_NO_CONTENT),
        (UnsupportedContentError("pdf"), MSG_UNSUPPORTED),
    ],
)
async def test_fetch_errors_get_friendly_reply(error, expected):
    bot, client = make_bot(fetch=AsyncMock(side_effect=error))
    await run_message(bot, make_event("https://example.org/a"))
    content = sent_content(client)
    assert content["body"] == expected
    assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$e1"


@pytest.mark.parametrize("error", [SummarizerError("api"), RuntimeError("unerwartet")])
async def test_ai_and_unexpected_errors_get_friendly_reply(error):
    bot, client = make_bot(
        fetch=AsyncMock(return_value=PAGE), summarize=AsyncMock(side_effect=error)
    )
    await run_message(bot, make_event("https://example.org/a"))
    assert sent_content(client)["body"] == MSG_AI_FAILED
    assert client.room_typing.await_args_list[-1].args[1] is False


async def test_send_failures_never_crash():
    bot, client = make_bot(
        fetch=AsyncMock(return_value=PAGE), summarize=AsyncMock(return_value="S.")
    )
    client.room_send = AsyncMock(side_effect=OSError("netz weg"))
    await run_message(bot, make_event("https://example.org/a"))
    client.room_send = AsyncMock(return_value=RoomSendError("M_FORBIDDEN"))
    await run_message(bot, make_event("https://example.org/a", event_id="$e2"))


async def test_fetch_concurrency_is_limited_to_three():
    active = 0
    peak = 0

    async def slow_fetch(url):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return PAGE

    bot, _ = make_bot(
        fetch=slow_fetch,
        summarize=AsyncMock(return_value="Satz."),
        env_extra={"RATE_LIMIT_PER_ROOM_PER_MINUTE": "50"},
    )
    for i in range(8):
        await bot.on_message(ROOM, make_event("https://example.org/a", event_id=f"$c{i}"))
    await asyncio.gather(*bot._tasks)
    assert peak == 3


async def test_sync_loop_is_not_blocked_by_slow_processing():
    gate = asyncio.Event()

    async def blocking_fetch(url):
        await gate.wait()
        return PAGE

    bot, client = make_bot(fetch=blocking_fetch, summarize=AsyncMock(return_value="S."))
    await bot.on_message(ROOM, make_event("https://example.org/a"))  # kehrt sofort zurück
    assert len(bot._tasks) == 1 and client.room_send.await_count == 0
    gate.set()
    await asyncio.gather(*bot._tasks)
    assert client.room_send.await_count == 1


async def test_task_exceptions_are_logged_not_swallowed(caplog):
    bot, client = make_bot()
    client.room_typing = AsyncMock()

    async def boom(*args):
        raise ValueError("kaputt")

    bot._process_urls = boom
    with caplog.at_level("ERROR"):
        await bot.on_message(ROOM, make_event("https://example.org/a"))
        await asyncio.gather(*bot._tasks, return_exceptions=True)
        await asyncio.sleep(0)
    assert any("kaputt" in (r.exc_text or "") for r in caplog.records)


async def test_logs_contain_domain_but_not_message_text(caplog):
    bot, _ = make_bot(fetch=AsyncMock(side_effect=FetchTimeoutError("t")))
    with caplog.at_level("INFO"):
        await run_message(bot, make_event("Mein Geheimnis https://example.org/pfad?token=SECRET"))
    text = caplog.text
    assert "example.org" in text and "$e1" in text and "!room:example.org" in text
    assert "Geheimnis" not in text and "SECRET" not in text


async def test_undecryptable_event_requests_key_and_never_crashes():
    bot, client = make_bot()
    client.request_room_key = AsyncMock(side_effect=RuntimeError("x"))
    event = SimpleNamespace(event_id="$m", session_id="S")
    await bot.on_undecryptable(ROOM, event)
    client.request_room_key.assert_awaited_once_with(event)


# ----------------------------------------------------------------- Auto-Join


def invite(sender, state_key=BOT, membership="invite"):
    return SimpleNamespace(sender=sender, state_key=state_key, membership=membership)


async def test_auto_join_accepts_any_invite_by_default():
    bot, client = make_bot()
    client.join = AsyncMock(return_value=SimpleNamespace(room_id="!r:x"))
    await bot.on_invite(MatrixRoom("!r:x", BOT), invite("@irgendwer:x"))
    client.join.assert_awaited_once_with("!r:x")


async def test_auto_join_respects_allowed_inviters():
    bot, client = make_bot(env_extra={"AUTO_JOIN_ALLOWED_INVITERS": "@kolja:x"})
    client.join = AsyncMock(return_value=SimpleNamespace(room_id="!r:x"))
    await bot.on_invite(MatrixRoom("!r:x", BOT), invite("@fremder:x"))
    client.join.assert_not_awaited()
    await bot.on_invite(MatrixRoom("!r:x", BOT), invite("@kolja:x"))
    client.join.assert_awaited_once()


async def test_invite_for_other_users_or_memberships_is_ignored():
    bot, client = make_bot()
    client.join = AsyncMock()
    await bot.on_invite(MatrixRoom("!r:x", BOT), invite("@a:x", state_key="@anderer:x"))
    await bot.on_invite(MatrixRoom("!r:x", BOT), invite("@a:x", membership="join"))
    client.join.assert_not_awaited()


async def test_join_errors_do_not_crash(monkeypatch):
    from nio import JoinError

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    bot, client = make_bot()
    client.join = AsyncMock(return_value=JoinError("M_FORBIDDEN"))
    await bot.on_invite(MatrixRoom("!r:x", BOT), invite("@a:x"))
    assert client.join.await_count == 3
    client.join = AsyncMock(side_effect=OSError("down"))
    await bot.on_invite(MatrixRoom("!r:x", BOT), invite("@a:x"))  # wirft nicht


# ----------------------------------------------------------------- Verifizierung


def make_verifier(auto_confirm=False):
    client = MagicMock()
    client.user_id = BOT
    client.device_id = "BOTDEVICE"
    client.should_query_keys = False
    client.to_device = AsyncMock(return_value=SimpleNamespace())
    client.keys_query = AsyncMock()
    handler = VerificationHandler(client, auto_confirm=auto_confirm, clock_ms=lambda: 5_000_000)
    return handler, client


def request_event(sender=BOT, **content_overrides):
    content = {
        "from_device": "ELEMENT",
        "methods": ["m.sas.v1", "m.qr_code.show.v1"],
        "transaction_id": "tx1",
        "timestamp": 5_000_000,
        **content_overrides,
    }
    return UnknownToDeviceEvent.from_dict(
        {"type": "m.key.verification.request", "sender": sender, "content": content}
    )


async def test_verification_request_from_own_user_is_answered_with_ready():
    handler, client = make_verifier()
    await handler.on_to_device(request_event())
    message = client.to_device.await_args.args[0]
    assert message.type == "m.key.verification.ready"
    assert (message.recipient, message.recipient_device) == (BOT, "ELEMENT")
    assert message.content == {
        "from_device": "BOTDEVICE",
        "methods": ["m.sas.v1"],
        "transaction_id": "tx1",
    }


async def test_verification_request_from_foreign_user_is_ignored():
    handler, client = make_verifier(auto_confirm=True)
    await handler.on_to_device(request_event(sender="@fremder:example.org"))
    client.to_device.assert_not_awaited()


async def test_stale_verification_request_is_ignored():
    handler, client = make_verifier()
    await handler.on_to_device(request_event(timestamp=1))
    client.to_device.assert_not_awaited()


async def test_request_without_sas_is_cancelled():
    handler, client = make_verifier()
    await handler.on_to_device(request_event(methods=["m.qr_code.show.v1"]))
    assert client.to_device.await_args.args[0].type == "m.key.verification.cancel"


async def test_handler_never_raises():
    handler, client = make_verifier()
    client.to_device = AsyncMock(side_effect=RuntimeError("kaputt"))
    await handler.on_to_device(request_event())  # darf nicht werfen


def own_sas(device_user=BOT):
    return SimpleNamespace(
        get_emoji=lambda: [("🐶", "Dog"), ("🐱", "Cat")],
        other_olm_device=SimpleNamespace(user_id=device_user, id="ELEMENT"),
        canceled=False,
        verified=True,
    )


def key_event(sender=BOT, tx="tx1"):
    from nio import KeyVerificationKey

    return KeyVerificationKey.from_dict(
        {
            "type": "m.key.verification.key",
            "sender": sender,
            "content": {"transaction_id": tx, "key": "a" * 43},
        }
    )


def mac_event(sender=BOT, tx="tx1"):
    from nio import KeyVerificationMac

    return KeyVerificationMac.from_dict(
        {
            "type": "m.key.verification.mac",
            "sender": sender,
            "content": {"transaction_id": tx, "keys": "k", "mac": {"ed25519:X": "m"}},
        }
    )


def start_event(sender, tx="tx9"):
    from nio import KeyVerificationStart

    return KeyVerificationStart.from_dict(
        {
            "type": "m.key.verification.start",
            "sender": sender,
            "content": {
                "from_device": "DEV",
                "transaction_id": tx,
                "method": "m.sas.v1",
                "key_agreement_protocols": ["curve25519-hkdf-sha256"],
                "hashes": ["sha256"],
                "message_authentication_codes": ["hkdf-hmac-sha256"],
                "short_authentication_string": ["emoji", "decimal"],
            },
        }
    )


@pytest.mark.parametrize("auto", [False, True])
async def test_auto_confirm_confirms_only_when_enabled_for_accepted_own_session(auto):
    handler, client = make_verifier(auto_confirm=auto)
    client.key_verifications = {"tx1": own_sas()}
    client.confirm_short_auth_string = AsyncMock()
    handler._peers["tx1"] = (BOT, "ELEMENT")  # von _on_request/_on_start akzeptiert
    await handler.on_to_device(key_event())
    assert client.confirm_short_auth_string.await_count == (1 if auto else 0)


async def test_key_event_for_unaccepted_transaction_is_never_confirmed():
    handler, client = make_verifier(auto_confirm=True)
    client.key_verifications = {"tx1": own_sas()}
    client.confirm_short_auth_string = AsyncMock()
    await handler.on_to_device(key_event())  # tx1 nie akzeptiert
    client.confirm_short_auth_string.assert_not_awaited()


async def test_key_event_from_foreign_sender_is_never_confirmed():
    handler, client = make_verifier(auto_confirm=True)
    client.key_verifications = {"tx1": own_sas()}
    client.confirm_short_auth_string = AsyncMock()
    handler._peers["tx1"] = (BOT, "ELEMENT")
    await handler.on_to_device(key_event(sender="@fremder:example.org"))
    client.confirm_short_auth_string.assert_not_awaited()


async def test_sas_with_foreign_device_is_never_confirmed_or_verified():
    handler, client = make_verifier(auto_confirm=True)
    client.key_verifications = {"tx1": own_sas(device_user="@fremder:example.org")}
    client.confirm_short_auth_string = AsyncMock()
    handler._peers["tx1"] = (BOT, "ELEMENT")
    await handler.on_to_device(key_event())
    await handler.on_to_device(mac_event())
    client.confirm_short_auth_string.assert_not_awaited()
    client.verify_device.assert_not_called()


async def test_foreign_start_discards_local_sas_so_later_events_do_nothing():
    """nio legt für jeden start eine SAS an. Ein Cancel allein reicht nicht."""
    handler, client = make_verifier(auto_confirm=True)
    foreign = "@fremder:example.org"
    client.key_verifications = {"tx9": own_sas(device_user=foreign)}
    client.confirm_short_auth_string = AsyncMock()

    await handler.on_to_device(start_event(foreign))
    assert "tx9" not in client.key_verifications
    assert client.to_device.await_args.args[0].type == "m.key.verification.cancel"

    # Böswilliger Client macht trotz Cancel weiter:
    await handler.on_to_device(key_event(sender=foreign, tx="tx9"))
    await handler.on_to_device(mac_event(sender=foreign, tx="tx9"))
    client.confirm_short_auth_string.assert_not_awaited()
    client.verify_device.assert_not_called()


async def test_full_own_session_flow_verifies_device_and_sends_done():
    handler, client = make_verifier(auto_confirm=True)
    client.key_verifications = {"tx1": own_sas()}
    client.confirm_short_auth_string = AsyncMock()
    await handler.on_to_device(request_event())  # request -> ready, tx1 wird akzeptiert
    await handler.on_to_device(key_event())
    await handler.on_to_device(mac_event())
    client.confirm_short_auth_string.assert_awaited_once_with("tx1")
    client.verify_device.assert_called_once()
    assert client.to_device.await_args.args[0].type == "m.key.verification.done"


async def test_room_alias_cannot_be_used_to_pass_the_room_filter():
    """canonical_alias ist von Raum-Admins frei setzbar und darf nicht autorisieren."""
    fetch = AsyncMock(return_value=PAGE)
    bot, _ = make_bot(fetch=fetch, env_extra={"ALLOWED_ROOMS": "#raum:example.org"})
    room = MatrixRoom("!boeser-raum:example.org", BOT)
    room.canonical_alias = "#raum:example.org"
    await bot.on_message(room, make_event("https://example.org/a"))
    await asyncio.gather(*bot._tasks)
    fetch.assert_not_awaited()
