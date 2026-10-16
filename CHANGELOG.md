# Changelog

All notable changes to this plugin are documented here.

## [0.3.0] - 2026-10-16
### Added
- Approval mode switch per session (chat composer): Ask (`default`), Edits (`acceptEdits`), **Auto** (`auto` — every tool call runs unless a deny rule blocks it or an ask rule flags it) and Plan; `bypassPermissions` stays admin-only because it also disables the guardrails. Bridge: `POST /sessions/{id}/permission-mode`, mode `auto` maps to the CLI's `default` so the policy is still consulted; a live CLI is switched in place.

## [0.2.0] - 2026-10-16
### Changed
- Instructions: `CLAUDE.md` no longer copies every plugin's `ai_instructions`. It carries the base instructions plus a generated **index** of the installed plugins (id, version, purpose, URL of the full guide) and the rule to fetch a plugin's guide from the running plugin before operating it, cached per version under `.lyndrix/cache/` (git-ignored). The pre-0.2 copies in `.lyndrix/instructions/` are removed on sync. Needs core API 1.12 for `ai_instructions_route`/`GET /api/ai/plugins`; on 1.11 only purposes are listed.
### Added
- Own full guide at `GET /ai/instructions` (served live from `ai/INSTRUCTIONS.md`), manifest `ai_instructions_route`.

## [0.1.6] - 2026-10-16
### Fixed
- Login: the token parsed from the CLI output could carry the next hint sentence glued to it (the TUI draws the following line with cursor moves, not newlines) — it was stored and every chat turn failed with `401 OAuth access token is invalid` while the settings page said "verified". The bridge now turns cursor moves into line breaks, validates a parsed token with one tiny headless turn before handing it out, and a failed validation reports the scrubbed output tail.
### Added
- `POST /auth/verify` (bridge + plugin): real check of the configured credential; the finisher verifies after a login and the settings page offers "Verify login". "Verified" is only shown after such a check — a present token is "configured".

## [0.1.5] - 2026-10-16
### Fixed
- Login: the code and Enter were written to the CLI in one chunk; the input treats ~90 characters arriving at once as a paste and swallowed the Enter — the CLI echoed the masked code and waited forever (seen live). Enter is now sent separately after 0.4 s and pressed again once if nothing happens within 10 s. Runner image 0.1.4.

## [0.1.4] - 2026-10-16
### Changed
- Login: the code exchange is asynchronous. `POST /auth/submit` answers 202, the runner exchanges the code in the background, the plugin claims the token once (`POST /auth/result`) and stores it; the UI polls `/auth/status` (`last_result`). Long requests through the load balancer were cut after about a minute ("Failed to fetch") and the one-time token was lost.
- Bridge logs the scrubbed CLI output tail when no token appears and also recognises "failed to …", "invalid_grant", "expired" and "unauthorized" as failures.

## [0.1.3] - 2026-10-16
### Fixed
- Runner: when the socket manager could not resolve the host path of the storage within 15 s the plugin fell back to the container path — Docker then created an empty directory on the host, the fingerprint flipped and the runner was recreated with a workspace without instructions. The lookup now waits 60 s and fails loudly (the watchdog retries); recreate log lines say what changed.

## [0.1.2] - 2026-10-16
### Fixed
- Login: the plugin's calls to the bridge used a 20 s timeout while the CLI may need up to 45 s for the URL and 90 s for the token exchange — a slow exchange ended in a 500 and the one-time token was lost. Timeouts are now aligned (UI ≥ plugin ≥ bridge) and connection errors while the container restarts answer 503 with Retry-After instead of a traceback.
- Runner: the first image pull may take longer than 180 s on a slow host; the spawn now waits up to 10 min.

## [0.1.1] - 2026-10-16
### Fixed
- Login: the authorization URL no longer carries the TUI hint text ("Hold Shift while selecting…") that the ANSI-stripped CLI output glued onto the `state` parameter — the pasted code failed the state check.
- Settings UI: starting the login waits up to 60 s (the CLI needs a moment to print the URL) and a timeout is reported as such instead of "signal is aborted without reason".
- Runner: explicit entrypoint command, persistent `/home/agent/.claude` volume, stable fingerprint across core restarts, serialized ensure + watchdog; sessions lost on a bridge restart are recreated with `resume`.

## [0.1.0] - 2026-10-16
### Added
- Claude Code as an in-app assistant: a long-lived sandboxed runner container (`lyndrix-ai-runner`) driven by an HTTP bridge built on the Claude Agent SDK, started on the instance with the `runner` role (placement `pinned:runner`, lease `ai_assistant:runner`).
- Chat as a sidebar tab (`/ai/chat`) and as a floating, per-user pinnable panel (core API 1.11 panels); settings/status page (`/ai/settings`).
- Login with the Claude subscription through the CLI's own OAuth flow from the settings page (URL + code), or with an Anthropic API key.
- Workspace: git repository on the shared storage (optional remote), `CLAUDE.md` generated from base instructions, every loaded plugin's `ai_instructions` and operator notes.
- Guardrails like the Claude Code app: permission mode plus allow / ask / deny rules per tool and pattern; interactive approvals in the chat.
- The assistant's own Lyndrix account (`ai-assistant`, bot) with an API key passed into the runner; allowed Vault spaces readable through the plugin API with audit logging.
- Sessions and transcripts in the database (`ai_sessions`, `ai_messages`).
