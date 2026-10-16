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
  Er hängt am Docker-Netzwerk der Core-Instanz und ist nur dort erreichbar. Er läuft als Benutzer
  `agent` (uid/gid 10000); der Core übergibt den Workspace-Baum nach jedem Sync an diesen Benutzer.
  Das benannte Volume `lyndrix-ai-runner-home` hält `/home/agent/.claude` (Claude-Konfiguration und
  Verlauf), damit eine Unterhaltung nach einem Neustart per `resume` fortgesetzt werden kann. Der
  Runner startet auch ohne Claude-Zugangsdaten — der Login läuft in ihm.
- **Sessions**: Die Bridge hält Sessions nur im Speicher. Nach einem Neustart legt das Plugin eine
  Session beim nächsten Senden unter derselben ID neu an (`resume` mit der gemerkten Claude-Session).
- **Workspace**: lokales Git-Repository auf dem geteilten Speicher; optional ein Remote
  (`workspace_repo_url`, Token nur pro Git-Befehl). `CLAUDE.md` wird generiert aus
  Grundinstruktionen, einem **Index der installierten Plugins** (Zweck aus `ai_instructions`,
  URL der vollständigen Anleitung aus `ai_instructions_route`, Core-API 1.12) und den
  Operator-Notizen (`extra_instructions`). Die Anleitung selbst liefert jedes Plugin live
  (`GET /api/plugins/<id>/ai/instructions`); der Assistent holt sie bei Bedarf und legt sie
  versioniert unter `.lyndrix/cache/` ab – es gibt keine kopierten Texte im Workspace.

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
Dateiänderungen im Workspace, `plan` verändert nichts, `bypassPermissions` erlaubt alles
(nur in der Sandbox). Zusätzlich drei Regellisten (`;`-getrennt, `Tool` oder `Tool: regex`
gegen Befehl bzw. Pfad): `guardrails_allow` läuft ohne Rückfrage, `guardrails_ask` fragt
immer, `guardrails_deny` lehnt ab. Auswertung in der Bridge: deny → allow → ask → Modus.
Eine Freigabe „Immer erlauben“ im Chat gilt für die Session.

## Zugriff des Agenten auf Lyndrix

Mit `agent_lyndrix_access` (Standard an) legt das Plugin den Benutzer `ai-assistant` (Flag
`bot`, Rolle `agent` des Plugins) an, erzeugt einen API-Key und übergibt `LYNDRIX_API_URL`
und `LYNDRIX_API_KEY` in den Container. Welche Rechte der Agent hat, entscheidet die
Benutzerverwaltung — Rollen und Gruppen wie bei jedem anderen Benutzer. Vault-Spaces
(`vault_spaces`, Komma-Liste unterhalb des `lyndrix`-Mounts) liest der Agent über
`GET /vault/spaces/<space>` des Plugins; jeder Zugriff wird protokolliert.

## API

| Methode | Pfad | Recht | Zweck |
|---|---|---|---|
| GET | `/status` | api:read | Runner, Auth, Workspace, Modus |
| GET | `/instructions` | api:read | generierte CLAUDE.md |
| POST | `/workspace/sync` | api:admin | Repo aktualisieren, Instruktionen neu erzeugen |
| POST | `/runner/restart` | api:admin | Container neu erzeugen |
| GET | `/runner/logs?tail=` | api:admin | Container-Log |
| POST/GET/DELETE | `/auth/start`, `/auth/submit`, `/auth/status`, `/auth/{id}` | api:admin | Claude-Login über die CLI (starten, Code senden, Stand, abbrechen) |
| GET/POST | `/sessions` | api:read / api:write | eigene Sessions (Admins: `?all=true`) |
| GET | `/sessions/{id}` | api:read | Session, Verlauf, Live-Zustand |
| POST | `/sessions/{id}/messages` | api:write | Nachricht senden (202) |
| GET | `/sessions/{id}/events?after=` | api:read | SSE-Strom der laufenden Runde |
| POST | `/sessions/{id}/approvals/{aid}` | api:write | allow / deny / allow_always |
| POST | `/sessions/{id}/interrupt`, DELETE `/sessions/{id}` | api:write | abbrechen, schließen |
| GET | `/vault/spaces`, `/vault/spaces/{space}` | api:read / api:admin | erlaubte Spaces, Werte |

Rollen: `viewer` (lesen), `user` (chatten, freigeben), `agent` (Konto des Assistenten),
`admin` (alles; automatisch für `INT_ADMIN`).

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
