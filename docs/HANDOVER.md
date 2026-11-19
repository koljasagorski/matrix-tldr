# matrix-tldr handover

Last updated: 2026-11-19. This document is for maintainers and other coding agents.
The repository is public: **do not add IP addresses, host names, credentials, or keys here.**
Concrete addresses and access details are kept privately by the operator.

## Code status

- Bot, SSRF protection, Anthropic/OpenAI providers, verification shim, and 214 tests are done.
  `pytest`, `ruff check`, `ruff format --check`, and `compileall` all pass.
- Three findings from an automated security review were fixed and covered by regression tests:
  room filter compares room IDs only (never aliases), decompression has a hard output cap (only
  gzip/deflate, raw bytes are counted), and SAS verification is limited to transactions the bot
  accepted from its own account (foreign users' local nio SAS sessions are discarded).

## Deployment

| Item | State |
| --- | --- |
| Host | Proxmox VE. Docker is **not** installed on the hypervisor. |
| Container | Dedicated unprivileged Debian 13 LXC with `nesting=1,keyctl=1`, 1 core, 1 GB RAM, 8 GB disk, DHCP, autostart |
| Docker | Docker CE and the Compose plugin from the official Docker repository |
| Project path in the LXC | `/opt/matrix-tldr` (clone of this repo, branch `main`) |
| Image | `matrix-tldr:latest`, built locally in the LXC with `docker compose build` |
| Volume | `matrix-tldr-store` (crypto store and session). **Never delete it.** |
| Configuration | `/opt/matrix-tldr/.env`, mode 0600, local only, **not in git** |
| Network | No LAN firewall around the container (the operator chose this). Only the bot's SSRF protection applies. |
| AI | Anthropic, `AI_MODEL=claude-haiku-5-5`, `AI_EFFORT=low` |
| Bot account | Created on the operator's own homeserver; credentials only in the LXC `.env` |
| Rooms | `ALLOWED_ROOMS` restricts the bot to the target room (room ID, no alias) |

Status: the bot is running and confirmed working by the operator.

## Operations

Update:

```bash
cd /opt/matrix-tldr
git pull --ff-only
docker compose up -d --build
docker compose logs -f --tail=50 matrix-tldr
```

The store volume survives updates. Never use `docker compose down -v`.

Rotate credentials by editing `/opt/matrix-tldr/.env` on the server and running
`docker compose up -d`. API keys and passwords must not go through chat tools or git.

## Learnings

- Some homeserver gateways mangle percent-encoded room aliases in the request path. If joining by
  alias fails with "not a legal room ID or room alias", resolve the alias directly against the
  target server's client API and join by room ID instead (`POST /rooms/{roomId}/join`).
- The room alias lookup and the join must not be confused with authorization: the bot filters by
  room ID only.

## Open items

1. The verification shim is not tested against every Element version. Fallback: compare the
   Ed25519 fingerprint manually (see README).
2. No cross-signing (matrix-nio limitation), so Element may show a shield next to the bot's
   messages.
3. Rotate any API key or secret that was ever pasted into a chat session.
4. Consider network-level isolation of the container from internal networks as a second line of
   defense behind the SSRF protection.

## Known limitations

- No pages behind logins or paywalls, no JavaScript rendering.
- Prompt injection is mitigated (delimiters, length and sentence limits) but not eliminated.
