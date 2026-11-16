# Changelog

All notable changes to this plugin are documented here.

## [0.4.1] - 2026-11-16
First installable release of the 0.4 line: 0.4.0 was tagged, but its runner image 0.2.0 was never published (the image
CI failed). No runtime code changed; runner image stays 0.2.0.

### Fixed
- Runner image CI: the test that checks how the real claude-agent-sdk hands the isolation flags to the CLI expected
  `--setting-sources ""` as two arguments. SDK 0.2.168 (pinned in `runner/requirements.txt`) writes the same thing as
  one argument, `--setting-sources=`. The test accepts both spellings and still fails when the flag is missing or
  carries a value. The isolation itself was intact.

### Added
- Runner image CI runs `runner/ci/isolation_probe.sh` inside the built image: a SessionStart hook in the workspace's
  `.claude/settings.json` must not run with empty setting sources (both spellings) and must run with
  `--setting-sources=project` (control). This checks the behaviour of the real CLI, not the SDK's internals.

## [0.4.0] - 2026-11-15
Security audit 2026-11. Needs runner image 0.2.0.

### Upgrade notes
- Grant "AI Assistant User" to everyone who should chat (and "Power User" for Auto).
- Publish runner image 0.2.0 before the plugin release.
  If the plugin runs first anyway, chat answers 503 and health is `degraded` (`runner_outdated`) until the image
  is there; the watchdog retries the pull every minute and recreates the runner on its own.
- Rebuild the React bundle.
- Recommendation, the owner decides: if a workspace remote was configured, its git token was readable by the agent in
  `.git/config`. The upgrade removes it from there but does not invalidate it.
- Disable the plugin before uninstalling it.
- All chat users must trust each other until per-user isolation exists.
- Minimum core stays 1.11.0 (tested against 1.11.1). Newer core APIs are used only behind `getattr`.

### Security
- Chatting (create, send, approve, switch mode, interrupt) needs the new permission `chat`. Roles `user`, `power_user` and `admin` have it. The global `api:write` baseline of `INT_USER` no longer opens the runner.
- The agent account `ai-assistant` is never plugin admin. Its role is `api:read` + `agent_vault`, and `api:admin` is removed.
- `bypassPermissions` is admin-only and `auto` needs `auto_mode`. Both are enforced on create, switch and restore; the operator default only applies when allowed.
- Runner bridge (runner 0.2.0):
  - secrets are removed from its environment and the process is non-dumpable;
  - the CLI gets the credentials explicitly, never the bridge token;
  - local requests are refused except on the token-free `/livez`;
  - its code is root-owned and the image has no HEALTHCHECK.
- Sessions load no workspace settings and no `CLAUDE.md`; instructions arrive as a system prompt from the plugin. `--strict-mcp-config` is passed when the CLI supports it. Login and verification run in a throwaway config dir.
- Guardrails:
  - a PreToolUse hook enforces deny/ask rules in every mode;
  - allow rules are anchored, never cover chained, substituted or redirected Bash commands, and never cover non-normalised paths;
  - built-in denies cover environment dumps, `/proc/*/environ`, credential variable names, `.git/` edits, git hooks/`hooksPath`/`fsmonitor` and `git --output`;
  - an edited input on approval is checked again.
- Workspace:
  - the core never follows agent symlinks and never runs git in the agent repository;
  - remote updates are merged inside the runner;
  - no token is stored in `.git/config`, and tokens left there by 0.3.0 clones are removed.
- Transcripts: token-shaped strings are redacted before they are stored.
- Disabling the plugin revokes the agent's API keys and then removes the runner container (keys first, so a slow
  `docker rm` cannot push the revocation past the core's teardown budget).
- **A runner bridge older than the pinned version is refused (fail-closed).** When the image could not be pulled, the
  previous container kept running and its old bridge answered "ok", so chat was served without the isolation of
  runner 0.2.0. New sessions, messages and approvals now answer 503, status and health show `runner_outdated`, and the
  watchdog keeps retrying. The same applies to a `runner_image` set to an older tag.
- The chat panel is offered only to users with the `chat` permission.
- Git commands the plugin runs in the workspace ignore `core.fsmonitor` and hooks from the agent-writable git config.

### Added
- `DELETE /sessions/{id}?purge=true`.
- Settings `transcript_retention_days`, `runner_memory`, `runner_cpus`, `runner_pids_limit`, and the action "Apply retention now".
- `GET /status` carries `caller` and `workspace.remote_credentials_removed`.

### Changed
- Default runner image is pinned to the tested runner version (0.2.0) instead of `:latest`. A stored `:latest` of the official image counts as the default.
- A recreate pulls the image first and keeps the old container when the pull fails.
- The approval card shows the full command. "Always allow" means exactly this call and is unavailable for long requests.

### Fixed
- Remote workspaces never fast-forwarded after the first sync.
- The SSE error frame was invalid JSON.
- Vault spaces with `/` could not be read.
- A revoked agent key kept being injected.
- An invalid `max_turns` caused a 500.
- Blocking DB reads ran on the event loop.
- A recorder cancelled by a reload lost the rest of the turn.
- Purging a running conversation left rows behind.

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
