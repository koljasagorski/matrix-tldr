# matrix-tldr

A Matrix bot that summarizes links in exactly two sentences. When someone posts an `https://`
link in a room, the bot fetches the page safely, extracts the main text with
[trafilatura](https://trafilatura.readthedocs.io/), asks an AI model for a summary, and replies
to the original message as a proper Matrix reply (`m.notice`). Encrypted rooms (E2EE) are
supported.

> **Language note:** the bot's summaries and its user-facing messages are written in **German**
> (the system prompt asks for German output). Log messages are German as well. Everything else,
> including this documentation, is in English.

Reply format: `🔗 example.org: <two-sentence summary>`

## Features

- Watches all joined rooms (optionally restricted to specific room IDs)
- End-to-end encryption with a persistent device and session (no new device per restart)
- SAS emoji device verification, accepted only from the bot's own account
- Strict SSRF protection (see [Security](#security-and-residual-risks))
- Per-room rate limit, typing indicator, at most 3 concurrent fetches
- Ignores its own messages, notices, edits, and events from before startup; de-duplicates events
- Auto-joins on invite (optionally only from allowed inviters)
- Providers: Anthropic (default), OpenAI, or any OpenAI-compatible endpoint
- Hardened Docker image (non-root, read-only filesystem, no capabilities)

## Requirements

1. A **bot account** on your homeserver (for example `@tldr:example.org`). Use it only for this bot.
2. An **API key** for your AI provider (`ANTHROPIC_API_KEY` by default).
3. Python 3.11+ for a local run, or Docker with the Compose plugin.

## Configuration

Copy `.env.example` to `.env` and fill it in. **Never commit `.env`**; it is git-ignored.

Required values:

| Variable | Meaning |
| --- | --- |
| `MATRIX_HOMESERVER` | Client API URL, e.g. `https://matrix.example.org` |
| `MATRIX_USER_ID` | Full bot ID, e.g. `@tldr:example.org` |
| `MATRIX_PASSWORD` | Only needed for the very first login (see below) |
| `AI_PROVIDER` | `anthropic` (default) or `openai` |
| `AI_MODEL` | Model name for the chosen provider (never hardcoded) |
| `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` | Key matching `AI_PROVIDER` |

Everything else is optional and documented in `.env.example` (timeouts, size limits, rate limit,
`ALLOWED_ROOMS`, `AUTO_JOIN_ALLOWED_INVITERS`, `VERIFY_AUTO_CONFIRM`, `LOG_LEVEL`, ...).

On the first start the bot logs in with `MATRIX_PASSWORD` and saves the session to
`store/session.json` (mode 0600). Every later start reuses that session, so no new device is
created. After the first successful start you can remove the password from `.env`.

`ALLOWED_ROOMS` takes **room IDs** (`!abc:server`, shown in Element under Room settings,
Advanced). Room aliases are intentionally not accepted: room admins can set aliases freely, so
they must never be used for authorization.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit it
python -m matrix_tldr
```

## Run with Docker

```bash
cp .env.example .env        # then edit it
docker compose up -d --build
docker compose logs -f matrix-tldr
```

The container runs as a non-root user (uid 10001) with a read-only root filesystem, all
capabilities dropped, `no-new-privileges`, a memory/PID limit, and log rotation. The crypto store
and session live in the named volume `matrix-tldr-store`.

Update with `git pull --ff-only && docker compose up -d --build`.

**Never run `docker compose down -v` and never delete the store volume.** It holds the bot's
device keys. Without it the bot cannot read earlier encrypted history, logs in as a new device,
and has to be verified again. Backups of the volume contain key material and must be protected.

## End-to-end encryption and device verification

The bot works in encrypted rooms **without** being verified: it sends to unverified devices.
Verifying the bot's device is optional but removes Element's "unverified session" warning for the
bot account's own session.

On startup the bot logs its device ID and Ed25519 fingerprint (look for the line containing
`Ed25519-Fingerprint`).

Limitation: matrix-nio does not implement cross-signing, so the bot cannot cross-sign its device.
Element may still show a shield next to the bot's messages even after the device is verified.

### Option A: emoji comparison (SAS)

1. Set `VERIFY_AUTO_CONFIRM=true` and restart the bot.
2. Log in to Element **as the bot account** (for example in a private browser window).
3. Open Settings, Sessions, select the session named `matrix-tldr`, and start emoji verification.
4. Compare the emojis Element shows with the emojis in the bot log. If they match, confirm in
   Element. The bot log then reports that the device is verified.
5. Set `VERIFY_AUTO_CONFIRM=false` and restart.

`VERIFY_AUTO_CONFIRM` only makes the *bot* confirm its side. The security comes from you
comparing the emojis in Element. The bot only accepts verification requests from its own account
and rejects everyone else. matrix-nio lacks the `request`/`ready`/`done` messages of the
verification flow, so `verification.py` implements them; this follows the spec but has not been
tested against every Element version.

### Option B (fallback): compare the fingerprint manually

In Element, open the bot account's session `matrix-tldr`, choose manual verification, and compare
the session key shown there with the Ed25519 fingerprint from the bot log
(`docker compose logs matrix-tldr | grep Fingerprint`). Confirm if they match.

### Check that it works in an encrypted room

Invite the bot to an encrypted room (it joins automatically; see `AUTO_JOIN_ALLOWED_INVITERS`) and
post an `https://` link. If the bot replies with `🔗 <domain>: ...`, sending and decrypting both
work. If the log shows an undecryptable message (German: `Nicht entschlüsselbar`), the room key
has not arrived yet; the bot requests it automatically and other clients normally share it when
they next send a message.

## Security and residual risks

- **SSRF protection** (`security.py`, `fetcher.py`): only `https` on port 443, no credentials in
  URLs, internal hostnames and single-label hosts blocked. Host names are resolved and **every**
  returned address is checked (IPv4 and IPv6): loopback, private ranges, link-local including
  cloud metadata, CGNAT, multicast, reserved and unspecified addresses, IPv4-mapped IPv6, NAT64,
  6to4, Teredo, and unusual IP notations (decimal, octal, hex).
- **DNS rebinding:** resolution, validation, and connection happen in one step. The bot connects
  to the validated IP while keeping the original host name for SNI, the `Host` header, and
  certificate verification.
- **Redirects** are followed manually (max. 5) and every hop is fully re-validated.
- **Resource limits:** the bot only advertises `gzip`/`deflate`, rejects other content encodings,
  and decompresses with a hard output cap, so a small compressed body cannot expand into a large
  one (zip bomb). Both the transferred and the decompressed size are limited.
- **Rate limit** per room (`RATE_LIMIT_PER_ROOM_PER_MINUTE`) bounds AI costs.
- **Prompt injection:** page text is wrapped in `<document>` tags and declared untrusted in the
  system prompt, tag-like content is neutralized, and the output is cut to two sentences and 600
  characters. This reduces the risk but cannot rule out a crafted page skewing a summary. Do not
  treat summaries as a security check of the linked page.
- **Residual risks:** the SSRF checks are enforced in the bot only. If you run it next to
  sensitive internal services, also block internal networks for the container at the network
  level. Page text is sent to your chosen AI provider; that is a privacy decision for your rooms.
  The bot cannot read pages behind logins or paywalls and does not execute JavaScript.
- Logs contain room IDs, event IDs, and the domain, but not message bodies or URL paths.

## Choosing a provider and model

Everything is configured in `.env`; nothing is hardcoded.

| Goal | Settings |
| --- | --- |
| Claude (default) | `AI_PROVIDER=anthropic`, `AI_MODEL=claude-haiku-5-5`, `ANTHROPIC_API_KEY` |
| Claude, stronger | `AI_MODEL=claude-sonnet-5-5` (noticeably more expensive, rarely needed) |
| OpenAI | `AI_PROVIDER=openai`, `AI_MODEL=<model>`, `OPENAI_API_KEY` |
| OpenAI-compatible (self-hosted or other vendor) | additionally `OPENAI_BASE_URL=https://.../v1` |

For a two-sentence summary a small, cheap model is plenty, so `claude-haiku-5-5` is the
recommended default: it gives the best price/quality ratio, and a summary costs a fraction of a
cent. Check your provider's current pricing before relying on a number. `AI_EFFORT=low` keeps
reasoning effort small; set `AI_EFFORT=none` for models that do not support the effort parameter.

## Development

```bash
pip install -r requirements-dev.txt && pip install -e .
pytest                                   # unit tests, no real API calls
ruff check . && ruff format --check . && python -m compileall -q src tests
```

Project layout:

```
src/matrix_tldr/
  __main__.py        entry point, logging, signals, exit codes
  config.py          loads and validates .env, collects all errors at once
  bot.py             Matrix logic: login, E2EE, filters, rate limit, replies, sync loop
  verification.py    SAS emoji device verification
  url_utils.py       link extraction from messages
  security.py        SSRF protection with DNS pinning
  fetcher.py         safe download, redirects, size limits, text extraction
  ai/                prompt and post-processing, Anthropic and OpenAI providers
tests/               pytest suite (no network access needed)
docs/HANDOVER.md     operations notes for maintainers
```

## Deployment notes

A maintainers' handover document lives in [`docs/HANDOVER.md`](docs/HANDOVER.md). The repository
is public: never put IP addresses, host names, credentials, or keys into it or into any commit.
