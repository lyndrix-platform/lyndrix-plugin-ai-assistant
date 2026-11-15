# KI-Assistent (lyndrix-plugin-ai-assistant)

Claude Code als Assistent in Lyndrix: ein Chat in der Shell (eigener Tab und schwebendes
Panel), Werkzeug-Freigaben wie in der Claude-Code-App, Wissen über die installierten Plugins
aus deren Manifesten, und ein eigener Lyndrix-Account für den Agenten.

## Architektur

```
Browser ──(SSE/REST)── Core-Instanz mit Rolle `runner` ──(HTTP, X-Bridge-Token)── lyndrix-ai-runner
                       ├─ Plugin-API /api/plugins/lyndrix.plugin.ai_assistant/        ├─ Claude Code CLI
                       ├─ Sessions/Verlauf in der DB (ai_sessions, ai_messages)      ├─ Bridge (FastAPI, Agent SDK)
                       └─ Workspace STORAGE_DIR/ai_workspace/repo ─────────────────── └─ /workspace (Mount)
```

- **Placement `pinned:runner`**: Nur Instanzen mit der Rolle `runner` laden das Plugin (Docker-
  Socket über den Core-Socket-Manager). Eine davon hält die Lease `ai_assistant:runner` und
  erzeugt/aktualisiert den Container; alle beantworten die API gegen denselben Container.
- **Runner-Container** `lyndrix-ai-runner` (Image `ghcr.io/lyndrix-platform/lyndrix-ai-runner`):
  langlebig, wird neu erzeugt, wenn Image, Zugangsdaten, Modell oder Workspace-Pfad wechseln.
  Standard-Image ist die mit dem Plugin getestete Runner-Version (`:<runner/VERSION>`), nicht
  `:latest`; ein gespeichertes `:latest` des offiziellen Images gilt als Standard. Vor dem
  Neuanlegen wird das Image gezogen: Scheitert das (Registry weg, falscher Tag), läuft der alte
  Container weiter. Speicher, CPU und Prozesse begrenzt `docker update` (Settings `runner_memory`
  4g, `runner_cpus` 2, `runner_pids_limit` 1024; 0 = aus). Härtung beim Start (`--cap-drop`,
  `no-new-privileges`, read-only, eigenes Netz, `--pull missing`) braucht `docker:spawn`-Optionen
  im Core und ist noch offen.
  Er hängt am Docker-Netzwerk der Core-Instanz und ist nur dort erreichbar. Er läuft als Benutzer
  `agent` (uid/gid 10000); der Core übergibt den Workspace-Baum nach jedem Sync an diesen Benutzer.
  Das benannte Volume `lyndrix-ai-runner-home` hält `/home/agent/.claude` (Claude-Konfiguration und
  Verlauf), damit eine Unterhaltung nach einem Neustart per `resume` fortgesetzt werden kann. Der
  Runner startet auch ohne Claude-Zugangsdaten — der Login läuft in ihm.
- **Sessions**: Die Bridge hält Sessions nur im Speicher. Nach einem Neustart legt das Plugin eine
  Session beim nächsten Senden unter derselben ID neu an (`resume` mit der gemerkten Claude-Session).
  Ein Plugin-Reload oder -Upgrade lässt den Runner laufen: Der Recorder speichert den bis dahin
  gestreamten Text, und die nächste Inkarnation hängt sich an noch laufende Runden wieder an.
  Sessions ohne laufende Runde werden auf `idle` gesetzt.
- **Deaktivieren** entfernt den Runner-Container dieses Hosts (samt Zugangsdaten in seiner
  Umgebung) und widerruft die API-Keys des Kontos `ai-assistant`. Beim Aktivieren wird ein neuer
  Key ausgestellt. Das Volume mit den Claude-Verläufen bleibt erhalten. **Vor dem Deinstallieren
  deaktivieren:** Der Core ruft bei Deinstallation denselben Teardown wie bei einem Reload auf,
  und der Runner bliebe sonst stehen.
- **Instruktionen**: Jede Session bekommt die Instruktionen als System-Prompt
  (`append_system_prompt`) direkt vom Plugin. Die CLI lädt keine Dateien aus dem Workspace
  (`setting_sources=[]`): weder `CLAUDE.md` noch `.claude/settings.json`. Der Workspace ist
  gemeinsam und für den Agenten schreibbar, eine Session könnte sonst Hooks, Allow-Regeln oder
  Anweisungen für alle anderen Sessions ablegen.
- **Workspace**: lokales Git-Repository auf dem geteilten Speicher; optional ein Remote
  (`workspace_repo_url`, Token nur pro Git-Befehl). Die Instruktionen (Kopie als `CLAUDE.md`) entstehen aus
  Grundinstruktionen, einem **Index der installierten Plugins** (Zweck aus `ai_instructions`,
  URL der vollständigen Anleitung aus `ai_instructions_route`, Core-API 1.12) und den
  Operator-Notizen (`extra_instructions`). Die Anleitung selbst liefert jedes Plugin live
  (`GET /api/plugins/<id>/ai/instructions`); der Assistent holt sie bei Bedarf und legt sie
  versioniert unter `.lyndrix/cache/` ab – es gibt keine kopierten Texte im Workspace.
- **Vertrauensgrenze Workspace**: `repo/` ist für den Agenten schreibbar. Der Core folgt dort
  keinem Symlink (Schreiben, Lesen, chown, Löschen über `safe_fs`) und führt kein `git` darin aus –
  die Git-Konfiguration eines Repos kann Programme starten. Ein Remote wird in einen nur für den
  Core sichtbaren Mirror (`ai_workspace/origin.git`) geholt; der erste Checkout entsteht in einem
  Staging-Verzeichnis, spätere Updates kommen als Bundle (`.lyndrix/cache/origin.bundle`) und
  werden **im Runner** als `agent` per `git pull --ff-only` übernommen (`docker exec`, Secrets
  geleert). Ohne Runner bleibt das Update in `status.workspace.update_pending` stehen. Im Repo
  steht nur die Remote-URL ohne Zugangsdaten.

## Anmeldung bei Claude

Zwei Wege, Standard ist die Subscription:

1. **Login über die Einstellungsseite**: „Mit Claude anmelden“ startet im Runner `claude
   setup-token`, die Anmelde-URL erscheint in der Oberfläche, der zurückgegebene Code wird
   eingetragen. Das Token landet verschlüsselt in Vault (Setting `claude_oauth_token`), der
   Runner startet damit neu.
2. **API-Key** (`claude_auth_mode = api_key`, `anthropic_api_key`): Abrechnung pro Token.

Im Container ist immer nur eine der beiden Variablen gesetzt; ein API-Key hätte sonst Vorrang
vor dem Subscription-Token.

## Werkzeug-Freigaben (Guardrails)

`permission_mode` wie in der App: `default` fragt bei jedem Werkzeug, `acceptEdits` erlaubt
Dateiänderungen im Workspace, `plan` verändert nichts, `bypassPermissions` fragt nie (Deny-Regeln
gelten weiter). Zusätzlich drei Regellisten (`;`-getrennt, `Tool` oder `Tool: regex` gegen Befehl
bzw. Pfad): `guardrails_allow` läuft ohne Rückfrage, `guardrails_ask` fragt immer,
`guardrails_deny` lehnt ab. Auswertung in der Bridge: eingebaute Deny-Regeln → deny → allow →
ask → Modus.
- Deny- und Ask-Regeln setzt ein **PreToolUse-Hook** für *jeden* Werkzeugaufruf in *jedem* Modus
  durch, auch dort, wo die CLI selbst nicht fragt (Lese-Werkzeuge, `acceptEdits`, Deny auch
  `bypassPermissions`). Sie treffen überall im Befehl, auch bei zusammengezogenen Leerzeichen.
- Allow-Regeln gelten ab dem Anfang des Befehls bzw. Pfads und decken nie einen Bash-Befehl
  mit `; & | ` $( < >` oder Zeilenumbruch ab: `git status; curl …` fällt nicht mehr unter
  `^git status`.
- Eingebaut und immer aktiv: kein Umgebungs-Dump (`env`, `printenv`, `export -p`), kein
  `/proc/*/environ`, keine Erwähnung der Claude-/Bridge-Credential-Variablen, keine Änderungen
  unter `.git/`. Das sind Sicherheitsgurte gegen Prompt-Injection, keine Grenze gegen einen
  entschlossenen Nutzer.
- Eine Freigabe „Immer erlauben“ im Chat gilt für die Session, und zwar nur für genau diesen
  Aufruf.
Der Modus lässt sich **pro Session im Chat umschalten** (Fragen · Bearbeiten · Auto · Plan):
`auto` erlaubt alles, was die Deny-/Ask-Regeln nicht stoppen. Es braucht das Recht `auto_mode`
(Rollen „AI Assistant Power User“ und Admin), weil dann kein Mensch mehr freigibt und
Prompt-Injection ungebremst wirkt. `bypassPermissions` fragt nie (Deny-Regeln gelten weiter) und
ist Administratoren vorbehalten. Beides wird beim Anlegen, beim Umschalten und beim Wiederherstellen
einer Session nach einem Runner-Neustart geprüft. Ein Operator-Default, den der Nutzer nicht
verwenden darf, startet die Session in `default`, und eine laufende Session in einem nicht
(mehr) erlaubten Modus wird beim nächsten Senden auf `default` zurückgesetzt.

## Zugriff des Agenten auf Lyndrix

Mit `agent_lyndrix_access` (Standard an) legt das Plugin den Benutzer `ai-assistant` (Flag
`bot`, Rolle `agent` des Plugins) an, erzeugt einen API-Key und übergibt `LYNDRIX_API_URL`
und `LYNDRIX_API_KEY` in den Container. Welche Rechte der Agent hat, entscheidet die
Benutzerverwaltung — Rollen und Gruppen wie bei jedem anderen Benutzer. Vault-Spaces
(`vault_spaces`, Komma-Liste unterhalb des `lyndrix`-Mounts) liest der Agent über
`GET /vault/spaces/<space>` des Plugins (Recht `agent_vault`); jeder Zugriff wird protokolliert.

**Wichtig – wer chatten darf, handelt mit diesem Konto.** Alle Sessions laufen im selben
Runner-Container unter derselben uid. Eine Werkzeug-Ausführung kann deshalb den Key des Agenten,
das Claude-Credential und die Werte der Vault-Spaces lesen. Daraus folgt:
- Chatten erfordert das eigene Recht `chat` (Rolle „AI Assistant User“ oder Admin). Die
  Core-Grundausstattung `api:write`, die jeder `INT_USER` auf jedes Plugin bekommt, reicht nicht.
- Das Konto `ai-assistant` ist nie Plugin-Admin (keine fremden Sessions, keine Runner-, Login- oder
  Workspace-Steuerung), auch wenn ein Operator ihm Admin-Rollen gibt. Ihm nur Rollen geben, die jeder
  Chat-Nutzer haben darf.
- Eine delegierte Identität (Agent handelt mit den Rechten des jeweiligen Nutzers) braucht einen Core
  mit begrenzten gescopten Keys (≥ 1.12.0) und Prozess-Isolation je Nutzer im Runner. Das ist noch
  offen.

## Verläufe

Sessions und Verlauf liegen in `ai_sessions`/`ai_messages`. Vor dem Speichern werden
Zeichenketten in bekannten Token-Formaten durch `[redacted]` ersetzt: Claude-/Anthropic-Keys,
Lyndrix-Keys, GitLab-/GitHub-/Vault-Tokens, `Bearer`-/`Basic`-Werte und private Schlüssel. Das
betrifft Nutzernachrichten, Werkzeug-Ein- und -Ausgaben und Ergebnisse. Allgemeine
„password=…“-Heuristiken gibt es bewusst nicht. `transcript_retention_days` löscht inaktive
Unterhaltungen nach N Tagen (alle 6 h auf dem Runner-Host; 0 = behalten, Standard). Laufende
Runden bleiben. Mit `?purge=true` löscht der Eigentümer oder ein Admin eine Unterhaltung sofort.

## API

| Methode | Pfad | Recht | Zweck |
|---|---|---|---|
| GET | `/status` | api:read | Runner, Auth, Workspace, Modus |
| GET | `/instructions` | api:read | Instruktionen des Agenten (System-Prompt, vom Plugin erzeugt) |
| POST | `/workspace/sync` | api:admin | Repo aktualisieren, Instruktionen neu erzeugen |
| POST | `/runner/restart` | api:admin | Container neu erzeugen |
| GET | `/runner/logs?tail=` | api:admin | Container-Log |
| POST/GET/DELETE | `/auth/start`, `/auth/submit`, `/auth/status`, `/auth/{id}` | api:admin | Claude-Login über die CLI (starten, Code senden, Stand, abbrechen) |
| GET/POST | `/sessions` | api:read / chat | eigene Sessions (Admins: `?all=true`) |
| GET | `/sessions/{id}` | api:read | Session, Verlauf, Live-Zustand |
| POST | `/sessions/{id}/messages` | chat | Nachricht senden (202) |
| GET | `/sessions/{id}/events?after=` | api:read | SSE-Strom der laufenden Runde |
| POST | `/sessions/{id}/approvals/{aid}` | chat | allow / deny / allow_always |
| POST | `/sessions/{id}/permission-mode`, `/sessions/{id}/interrupt` | chat | Modus wechseln, abbrechen |
| DELETE | `/sessions/{id}` | api:write | schließen (archivieren); `?purge=true` löscht Session und Verlauf endgültig |
| POST | `/maintenance/prune` | api:admin | Aufbewahrungsfrist (`transcript_retention_days`) jetzt anwenden |
| GET | `/vault/spaces`, `/vault/spaces/{space}` | api:read / agent_vault | erlaubte Spaces, Werte |

Session-Routen gelten nur für eigene Sessions; Admins sehen alle. Admin-Routen und `chat`
verweigern das Konto `ai-assistant` immer.

Rollen: `viewer` (lesen), `user` (chatten, freigeben: `chat`), `power_user` (wie `user` plus
`auto_mode`), `agent` (Konto des Assistenten: `api:read` + `agent_vault`), `admin` (alles;
automatisch für `INT_ADMIN`).

## Entwicklung

```bash
# Tests (im Dev-Core-Container, Plugin als Paket ai_plugin)
tar --exclude=node_modules --exclude=.git --exclude=runner -cf - . | docker exec -i lyndrix-core-dev sh -c 'rm -rf /tmp/t-ai && mkdir -p /tmp/t-ai/ai_plugin && tar xf - -C /tmp/t-ai/ai_plugin'
docker exec -w /app -e PYTHONPATH=/app:/tmp/t-ai lyndrix-core-dev python -m pytest /tmp/t-ai/ai_plugin/tests -q
# React-Bundle
docker run --rm -u $(id -u):$(id -g) -v $PWD:/w -w /w node:20-slim sh -c 'npm install && npm run build'
# Runner-Image lokal
docker build -t lyndrix-ai-runner:dev runner/
```

Grenzen der ersten Version: ein Workspace für alle Benutzer (Sessions getrennt, Dateien
nicht); nur Provider `claude-code`; Hermes (für API-Key-Modelle) folgt als zweiter Provider.
