# Übergabestand matrix-tldr

Stand: 2026-11-19. Dieses Dokument ist für andere Coding-Agenten und Betreiber gedacht.
Das Repo ist öffentlich: **keine IPs, Hostnamen, Zugangsdaten oder Schlüssel hier eintragen.**
Konkrete Zugangsdaten und Adressen stehen nur beim Betreiber (privat).

## Code-Stand

- Bot, SSRF-Schutz, Claude-/OpenAI-Anbindung, Verifizierungs-Shim und 214 Tests sind fertig
  (`pytest`, `ruff check`, `ruff format --check`, `compileall` laufen sauber).
- Drei Findings des automatischen Security-Reviews sind behoben und mit Regressionstests
  abgesichert: Raumfilter nur per Raum-ID (kein Alias), Dekompression mit harter Obergrenze
  (nur gzip/deflate, Rohbytes werden gezählt), SAS-Verifizierung nur für akzeptierte
  Eigenkonto-Transaktionen (lokale nio-SAS fremder Nutzer wird verworfen).

## Server-Setup (Homelab)

| Punkt | Stand |
| --- | --- |
| Host | Proxmox VE (x86_64). Docker läuft **nicht** auf dem Hypervisor. |
| Container | Eigener unprivilegierter LXC, Debian 13, `nesting=1,keyctl=1`, 1 Kern, 1 GB RAM, 8 GB Disk, DHCP, Autostart |
| Docker | Docker CE + Compose-Plugin aus dem offiziellen Docker-Repo, per systemd aktiv |
| Projektpfad im LXC | `/opt/matrix-tldr` (Klon dieses Repos, Branch `main`) |
| Image | `matrix-tldr:latest`, lokal im LXC gebaut (`docker compose build`) |
| Volume | `matrix-tldr-store` (Crypto-Store und Session). **Nie löschen**, siehe README |
| Konfiguration | `/opt/matrix-tldr/.env`, Rechte 0600, nur lokal, **nicht im Git** |
| Netz | Keine Firewall-Sperre zum LAN (bewusste Entscheidung des Betreibers). Es gilt nur der SSRF-Schutz im Bot |
| Homeserver | `https://m.sgr.ski` (öffentlicher Homeserver-Name, steht in der `.env`) |
| KI | Anthropic, `AI_MODEL=claude-haiku-5-5`, `AI_EFFORT=low` |

Geprüft im LXC: Docker-Smoke-Test mit `--read-only --cap-drop ALL --no-new-privileges`, Store-Volume
als uid 10001 beschreibbar, Root-Dateisystem schreibgeschützt, Konfigurationsfehler bei
unvollständiger `.env` liefert Exit-Code 2 mit klarer deutscher Meldung.

## Noch offen (Stand dieses Dokuments)

1. Bot-Konto auf dem Homeserver muss existieren. `MATRIX_USER_ID` und `MATRIX_PASSWORD` sowie
   `ANTHROPIC_API_KEY` trägt der Betreiber selbst in die `.env` im LXC ein (nicht über Chat/Git).
2. Danach starten: `cd /opt/matrix-tldr && docker compose up -d` und `docker compose logs -f`.
3. Beim ersten Start Geräte-ID und Ed25519-Fingerprint aus dem Log notieren, Gerät in Element
   verifizieren (README, Abschnitt E2EE).
4. Ziel-Raum steht noch nicht fest. Betreiber nennt ihn später. Dann Raum-ID in `ALLOWED_ROOMS`
   eintragen (keine Aliase), Bot einladen, Container neu starten.
5. Der Verifizierungs-Shim ist nicht gegen echte Element-Versionen getestet. Fallback ist der
   manuelle Fingerprint-Vergleich (README).

## Update-Ablauf im LXC

```bash
cd /opt/matrix-tldr
git pull --ff-only
docker compose up -d --build
docker compose logs -f --tail=50 matrix-tldr
```

Das Store-Volume bleibt dabei erhalten. Nie `docker compose down -v` verwenden.

## Bekannte Einschränkungen

- Kein Cross-Signing (matrix-nio unterstützt es nicht): Element kann den Bot als nicht
  cross-signiert markieren, auch wenn das Gerät verifiziert ist.
- Keine Seiten hinter Login/Paywall, kein JavaScript-Rendering.
- Prompt-Injection wird gemindert (Delimiter, Längen- und Satzbegrenzung), aber nicht ausgeschlossen.
- Linear-Projektzuordnung ist in dieser Sitzung nicht erfolgt, weil der Linear-Zugang nicht
  authentifiziert war.
