# Changelog

All notable changes to this plugin are documented here.

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
