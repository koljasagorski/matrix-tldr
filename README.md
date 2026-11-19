# matrix-tldr

Matrix-Bot, der Links in Räumen auf Deutsch in genau zwei Sätzen zusammenfasst. Postet jemand
einen `https://`-Link, ruft der Bot die Seite sicher ab, extrahiert den Haupttext (trafilatura),
lässt eine KI zusammenfassen und antwortet als echte Matrix-Antwort (Reply) auf die Nachricht.
Verschlüsselte Räume (E2EE) werden unterstützt.

## Voraussetzungen

1. **Bot-Account** auf dem Homeserver anlegen (z. B. `@tldr:example.org`). Der Account wird
   nur vom Bot genutzt.
2. **API-Key** besorgen. Standard ist Claude (`ANTHROPIC_API_KEY`), alternativ OpenAI oder ein
   OpenAI-kompatibler Endpunkt.
3. Python 3.11+ (lokal) oder Docker.

## Lokaler Start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt        # oder requirements.txt ohne Test-Werkzeuge
cp .env.example .env                       # Werte eintragen, siehe unten
python -m matrix_tldr
```

Beim ersten Start meldet sich der Bot mit `MATRIX_PASSWORD` an und speichert die Session
(`store/session.json`, Rechte 0600). Jeder weitere Start nutzt diese Session, es entsteht
**kein** neues Gerät. Danach kann das Passwort aus der `.env` entfernt werden.

Pflichtwerte in der `.env`: `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_PASSWORD` (nur
erster Start), `AI_PROVIDER`, `AI_MODEL` und der passende API-Key.

## Docker-Start

```bash
cp .env.example .env                       # Werte eintragen
docker compose up -d --build
docker compose logs -f matrix-tldr
```

Der Container läuft als Non-Root-User, mit read-only Dateisystem, ohne Capabilities und mit
Log-Rotation. Crypto-Store und Session liegen im benannten Volume `matrix-tldr-store`.

## E2EE: Store-Volume und Geräteverifizierung

**Das Store-Volume (`matrix-tldr-store` bzw. `./store`) darf nie gelöscht werden.** Es enthält die
Geräteschlüssel des Bots. Geht es verloren, kann der Bot bisherige verschlüsselte Nachrichten nicht
mehr lesen, meldet sich als neues Gerät an und muss neu verifiziert werden. Backups des Volumes
enthalten Schlüsselmaterial und sind entsprechend zu schützen.

Beim Start loggt der Bot Geräte-ID und Ed25519-Fingerprint. Der Bot funktioniert auch **ohne**
Verifizierung (er sendet an unverifizierte Geräte). Verifizieren lohnt sich, damit Element die
Session nicht als unverifiziert warnt. Hinweis: matrix-nio unterstützt kein Cross-Signing. Der
Bot kann seine Nachrichten daher nicht selbst als "verifiziert" signieren, Mitglieder können in
Element trotzdem einen Hinweis zum Absendergerät sehen.

Verifiziert wird **eine Session des Bot-Kontos** (nicht von Fremdkonten):

### Variante A: Emoji-Vergleich (SAS)

1. In `.env` `VERIFY_AUTO_CONFIRM=true` setzen und den Bot (neu) starten.
2. Mit dem **Bot-Konto** in Element anmelden (z. B. im Browser).
3. Einstellungen, Sitzungen: die Sitzung `matrix-tldr` öffnen und "Sitzung verifizieren" wählen
   (Beschriftung je nach Element-Version leicht abweichend), Emoji-Verifizierung starten.
4. Die Emojis in Element mit dem Bot-Log vergleichen. Stimmen sie überein, in Element bestätigen.
   Das Log meldet dann "Gerät ... ist jetzt verifiziert".
5. `VERIFY_AUTO_CONFIRM=false` setzen und den Bot neu starten.

Der Bot nimmt Verifizierungsanfragen ausschließlich vom eigenen Bot-Konto an, auch bei
`VERIFY_AUTO_CONFIRM=true`. Die eigentliche Prüfung ist der Emoji-Vergleich durch dich in Element.
nio kennt `request`/`ready`/`done` nicht; `verification.py` ergänzt sie. Das ist gegen die
Protokollbeschreibung implementiert, aber nicht gegen jede Element-Version getestet.

### Variante B (Fallback): manuell per Fingerprint

Funktioniert Variante A nicht, geht es ohne den Bot-Code: In Element beim Bot-Konto in den
Sitzungen `matrix-tldr` wählen, "Manuell verifizieren" öffnen und den dort angezeigten
Sitzungsschlüssel mit dem **Ed25519-Fingerprint aus dem Bot-Log** vergleichen
(`docker compose logs matrix-tldr | grep Fingerprint`). Stimmen beide überein, bestätigen.

### Funktioniert der Bot in verschlüsselten Räumen?

Bot in einen verschlüsselten Raum einladen (er tritt automatisch bei, siehe
`AUTO_JOIN_ALLOWED_INVITERS`) und einen `https://`-Link posten. Antwortet der Bot mit
"🔗 domain: ...", funktioniert Senden und Lesen. Erscheint im Log "Nicht entschlüsselbar", fehlt
der Raumschlüssel: Der Bot fordert ihn automatisch an, und Element-Clients teilen ihn in der Regel
beim nächsten Nachrichtenversand.

## Sicherheit und Restrisiken

- **SSRF-Schutz** (`security.py`): nur `https` auf Port 443, keine Zugangsdaten in URLs. Hostnamen
  werden aufgelöst und **alle** Adressen geprüft (IPv4 und IPv6). Blockiert werden Loopback,
  private Netze, Link-Local inkl. Cloud-Metadata, CGNAT, Multicast, reservierte und
  unspezifizierte Adressen, IPv4-mapped IPv6 sowie Sonderschreibweisen (dezimal, oktal, hex).
  Redirects werden manuell verfolgt (max. 5), jedes Ziel wird erneut vollständig geprüft.
- **DNS-Rebinding**: Auflösung, Prüfung und Verbindung sind ein Schritt; der Bot verbindet sich mit
  der geprüften IP, SNI und `Host` bleiben der Hostname, das Zertifikat wird gegen den Namen geprüft.
- **Rate-Limit** pro Raum (`RATE_LIMIT_PER_ROOM_PER_MINUTE`) begrenzt die KI-Kosten.
- **Prompt-Injection**: Der Webseitentext steht in `<document>`-Tags und ist als nicht
  vertrauenswürdig markiert; die Ausgabe wird auf zwei Sätze und 600 Zeichen begrenzt. Das mindert
  das Risiko, schließt aber nicht aus, dass eine präparierte Seite die Zusammenfassung verfälscht.
  Zusammenfassungen sind keine verlässliche Prüfung des Seiteninhalts.
- **Restrisiken**: Der Bot kann keine Seiten hinter Login oder Paywall lesen. Der Schutz gilt nicht
  für Ziele, auf die der Host über eine Netzwerkregel umgeleitet wird. Sinnvoll ist zusätzlich,
  den Container per Firewall an interne Netze nicht heranzulassen. Der Bot sendet Seitentext an den
  gewählten KI-Anbieter; das ist eine Datenschutzentscheidung für deine Räume.
- Logs enthalten Raum-ID, Event-ID und Domain, aber keine Nachrichteninhalte oder URL-Pfade.

## Provider und Modell wechseln

Alles steht in der `.env`, nichts im Code.

| Ziel | Einstellung |
| --- | --- |
| Claude (Standard) | `AI_PROVIDER=anthropic`, `AI_MODEL=claude-haiku-5-5`, `ANTHROPIC_API_KEY` |
| Claude, stärker | `AI_MODEL=claude-sonnet-5-5` (ca. 20-mal teurer, selten nötig) |
| OpenAI | `AI_PROVIDER=openai`, `AI_MODEL=<Modell>`, `OPENAI_API_KEY` |
| OpenAI-kompatibel (selbst gehostet, EU-Anbieter) | zusätzlich `OPENAI_BASE_URL=https://.../v1` |

**Modellwahl:** Für eine Zwei-Satz-Zusammenfassung ist `claude-haiku-5-5` (0,10 $ / 0,50 $ pro
Million Token) das beste Preis-Leistungs-Verhältnis. Eine Zusammenfassung mit rund 4.000
Eingabe-Token kostet damit ungefähr 0,05 Cent. `AI_EFFORT=low` hält den Denkaufwand klein. Bei
Modellen ohne Effort-Unterstützung `AI_EFFORT=none` setzen.

## Entwicklung

```bash
pip install -r requirements-dev.txt && pip install -e .
pytest            # Unit-Tests, keine echten API-Aufrufe
ruff check . && python -m compileall -q src tests
```
