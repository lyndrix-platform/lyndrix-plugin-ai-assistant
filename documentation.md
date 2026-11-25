# AI Assistant (lyndrix-plugin-ai-assistant)

Claude Code as an assistant in Lyndrix: a chat in the shell (its own tab and a floating
panel), tool approvals as in the Claude Code app, knowledge of the installed plugins
from their manifests, and a dedicated Lyndrix account for the agent.

## Architecture

```
Browser ──(SSE/REST)── Core instance with role `runner` ──(HTTP, X-Bridge-Token)── lyndrix-ai-runner
                       ├─ Plugin API /api/plugins/lyndrix.plugin.ai_assistant/        ├─ Claude Code CLI
                       ├─ Sessions/history in the DB (ai_sessions, ai_messages)      ├─ Bridge (FastAPI, Agent SDK)
                       └─ Workspace STORAGE_DIR/ai_workspace/repo ─────────────────── └─ /workspace (mount)
```

- **Placement `pinned:runner`**: Only instances with the role `runner` load the plugin (Docker
  socket via the core socket manager). One of them holds the lease `ai_assistant:runner` and
  creates/updates the container; all of them answer the API against the same container.
- **Runner container** `lyndrix-ai-runner` (image `ghcr.io/lyndrix-platform/lyndrix-ai-runner`):
  long-lived, recreated when the image, credentials, model or workspace path change.
  The default image is the runner version tested with the plugin (`:<runner/VERSION>`), not
  `:latest`; a stored `:latest` of the official image counts as the default. Before recreation the
  image is pulled: if that fails (registry gone, wrong tag), the old container keeps running.
  **Fail-closed:** If the bridge is older than the pinned runner version (pull failed, old
  container still running, or `runner_image` set to an older tag), new sessions, messages and
  approvals answer with 503; `/status` and the health check report `runner_outdated` (`degraded`),
  and the watchdog retries the pull every minute. The runner image must therefore be published
  **before** the plugin release. `docker update` limits memory, CPU and processes (settings `runner_memory`
  4g, `runner_cpus` 2, `runner_pids_limit` 1024; 0 = off). Hardening at start (`--cap-drop`,
  `no-new-privileges`, read-only, own network, `--pull missing`) requires `docker:spawn` options
  in the core and is still open.
  It is attached to the Docker network of the core instance and is only reachable there. It runs as user
  `agent` (uid/gid 10000); the core hands the workspace tree to this user after every sync.
  The named volume `lyndrix-ai-runner-home` holds `/home/agent/.claude` (Claude configuration and
  history), so that a conversation can be continued via `resume` after a restart. The runner also starts
  without Claude credentials — the login runs inside it.
- **Sessions**: The bridge keeps sessions in memory only. After a restart the plugin re-creates a
  session under the same ID on the next send (`resume` with the remembered Claude session).
  A plugin reload or upgrade leaves the runner running: the recorder stores the text streamed so far,
  and the next incarnation reattaches to rounds that are still running.
  Sessions without a running round are set to `idle`.
- **Disabling** removes the runner container of this host (including the credentials in its
  environment) and revokes the API keys of the account `ai-assistant`. Enabling issues a new
  key. The volume with the Claude histories is preserved. **Disable before uninstalling:**
  on uninstall the core runs the same teardown as on a reload, and the runner would otherwise keep running.
- **Instructions**: Every session receives the instructions as system prompt
  (`append_system_prompt`) directly from the plugin. The CLI does not load any files from the workspace
  (`setting_sources=[]`): neither `CLAUDE.md` nor `.claude/settings.json`. The workspace is
  shared and writable for the agent; otherwise one session could place hooks, allow rules or
  instructions for all other sessions.
- **Workspace**: local Git repository on the shared storage; optionally a remote
  (`workspace_repo_url`, token only per Git command). The instructions (copy as `CLAUDE.md`) are built from
  base instructions, an **index of the installed plugins** (purpose from `ai_instructions`,
  URL of the full guide from `ai_instructions_route`, core API 1.12) and the
  operator notes (`extra_instructions`). Each plugin itself provides the guide live
  (`GET /api/plugins/<id>/ai/instructions`); the assistant fetches it when needed and stores it versioned
  under `.lyndrix/cache/` – there are no copied texts in the workspace.
- **Trust boundary workspace**: `repo/` is writable for the agent. The core does not follow
  any symlink there (write, read, chown, delete via `safe_fs`) and does not run `git` inside it –
  the Git configuration of a repo can start programs. A remote is fetched into a mirror visible only to the core
  (`ai_workspace/origin.git`); the first checkout is created in a staging directory, later updates arrive as a bundle
  (`.lyndrix/cache/origin.bundle`) and are applied **in the runner** as `agent` via `git pull --ff-only` (`docker exec`,
  secrets cleared). Without a runner the update remains at `status.workspace.update_pending`. The repo
  contains only the remote URL without credentials.

## Signing in to Claude

Two ways, the default is the subscription:

1. **Login via the settings page**: "Sign in with Claude" starts `claude setup-token` in the runner,
   the sign-in URL appears in the UI, and the returned code is entered. The token ends up encrypted in Vault
   (setting `claude_oauth_token`), and the runner restarts with it.
2. **API key** (`claude_auth_mode = api_key`, `anthropic_api_key`): billing per token.

In the container only one of the two variables is ever set; otherwise an API key would take
precedence over the subscription token.

## Tool approvals (guardrails)

`permission_mode` as in the app: `default` asks for every tool, `acceptEdits` allows file changes in the workspace, `plan` changes nothing, `bypassPermissions` never asks (deny rules still apply). In addition, three rule lists (`;`-separated, `Tool` or `Tool: regex` against command or path):
`guardrails_allow` runs without asking, `guardrails_ask` always asks,
`guardrails_deny` rejects. Evaluation in the bridge: built-in deny rules → deny → allow →
ask → mode.
- Deny and ask rules are enforced by a **PreToolUse hook** for *every* tool call in *every* mode,
  including where the CLI itself does not ask (read tools, `acceptEdits`, deny also under
  `bypassPermissions`). They match anywhere in the command, including with collapsed whitespace.
- Allow rules apply from the start of the command or path and never cover a Bash command
  containing `; & | ` $( < >` or a line break: `git status; curl …` no longer falls under
  `^git status`. Anchor the end as well (`$`), otherwise arguments are carried along
  (`git log --output=FILE` writes files). File paths are additionally checked for deny/ask in
  normal form (`..`, `.`, `//`); allow only applies to paths that are already normal.
- Built in and always active: no environment dump (`env`, `printenv`, `export`, `set`,
  `declare -p`), no `/proc/*/environ`, no mention of the Claude/bridge credential variables,
  no changes under `.git/`, no Git hooks, no `core.hooksPath`/`core.fsmonitor`, no `git … --output`. These are safety belts against prompt injection, not a boundary against a determined user.
- An "Always allow" approval in the chat applies to the session, and only to exactly this
  call.
The mode can be **switched per session in the chat** (Ask · Edit · Auto · Plan):
`auto` allows everything that the deny/ask rules do not stop. It requires the right `auto_mode`
(roles "AI Assistant Power User" and Admin), because then no human approves anymore and
prompt injection acts unchecked. `bypassPermissions` never asks (deny rules still apply) and
is reserved for administrators. Both are checked when creating, when switching and when restoring
a session after a runner restart. An operator default that the user may not
use starts the session in `default`, and a running session in a mode that is not (or no longer)
allowed is reset to `default` on the next send.

## Agent access to Lyndrix

With `agent_lyndrix_access` (default on) the plugin creates the user `ai-assistant` (flag
`bot`, role `agent` of the plugin), generates an API key and passes `LYNDRIX_API_URL`
and `LYNDRIX_API_KEY` into the container. Which rights the agent has is decided by user
management — roles and groups as for any other user. The agent reads Vault spaces
(`vault_spaces`, comma-separated list below the `lyndrix` mount) via
`GET /vault/spaces/<space>` of the plugin (right `agent_vault`); every access is logged.

**Important – whoever may chat acts with this account.** All sessions run in the same
runner container under the same uid. A tool execution can therefore read the agent's key,
the Claude credential and the values of the Vault spaces. It follows that:
- Chatting requires the own right `chat` (role "AI Assistant User" or Admin). The core base permission
  `api:write`, which every `INT_USER` receives on every plugin, is not sufficient.
- The account `ai-assistant` is never plugin admin (no foreign sessions, no runner, login or
  workspace control), even if an operator gives it admin roles. Only give it roles that every
  chat user may have.
- **All chat users must trust each other.** Under the same uid a tool execution can interfere with
  running sessions of other users: write control and user messages via `/proc/<pid>/fd/0` of the foreign CLI, and read all Claude histories
  under `~/.claude/projects/`. The bridge itself (approvals, tokens) is protected against this.
- A delegated identity (agent acts with the rights of the respective user) requires a core with scoped keys
  with limited scope (≥ 1.12.0) and process isolation per user in the runner (own uid
  and own `CLAUDE_CONFIG_DIR` per user). This is still open.

## Upgrade to 0.4.x

- Grant the right `chat` (role "AI Assistant User", for Auto "Power User") to everyone who should
  chat; `api:write` alone is no longer sufficient. The chat panel only appears with `chat`.
- Publish runner image 0.2.0 beforehand (see fail-closed above) and rebuild the React bundle.
- If the workspace had a remote, its Git token was readable for the agent in `.git/config`.
  The upgrade removes it there (`status.workspace.remote_credentials_removed`), but does not revoke it: the owner decides on a rotation.
- Disable before uninstalling. `GET /status` additionally returns `caller`.
- Minimum core remains 1.11.0.

## History

Sessions and history are stored in `ai_sessions`/`ai_messages`. Before saving,
strings in known token formats are replaced by `[redacted]`: Claude/Anthropic keys,
Lyndrix keys, GitLab/GitHub/Vault tokens, `Bearer`/`Basic` values and private keys. This
applies to user messages, tool inputs and outputs, and results. General
"password=…" heuristics are deliberately not included. `transcript_retention_days` deletes inactive
conversations after N days (every 6 h on the runner host; 0 = keep, default). Running
rounds remain. With `?purge=true` the owner or an admin deletes a conversation immediately.

## API

| Method | Path | Right | Purpose |
|---|---|---|---|
| GET | `/status` | api:read | Runner, auth, workspace, mode |
| GET | `/instructions` | api:read | Agent instructions (system prompt, generated by the plugin) |
| POST | `/workspace/sync` | api:admin | Update repo, regenerate instructions |
| POST | `/runner/restart` | api:admin | Recreate container |
| GET | `/runner/logs?tail=` | api:admin | Container log |
| POST/GET/DELETE | `/auth/start`, `/auth/submit`, `/auth/status`, `/auth/{id}` | api:admin | Claude login via the CLI (start, send code, status, cancel) |
| GET/POST | `/sessions` | api:read / chat | own sessions (admins: `?all=true`) |
| GET | `/sessions/{id}` | api:read | session, history, live state |
| POST | `/sessions/{id}/messages` | chat | send message (202) |
| GET | `/sessions/{id}/events?after=` | api:read | SSE stream of the running round |
| POST | `/sessions/{id}/approvals/{aid}` | chat | allow / deny / allow_always |
| POST | `/sessions/{id}/permission-mode`, `/sessions/{id}/interrupt` | chat | switch mode, cancel |
| DELETE | `/sessions/{id}` | api:write | close (archive); `?purge=true` permanently deletes session and history |
| POST | `/maintenance/prune` | api:admin | apply retention period (`transcript_retention_days`) now |
| GET | `/vault/spaces`, `/vault/spaces/{space}` | api:read / agent_vault | allowed spaces, values |

Session routes only apply to one's own sessions; admins see all. Admin routes and `chat`
always deny the account `ai-assistant`.

Roles: `viewer` (read), `user` (chat, approve: `chat`), `power_user` (like `user` plus
`auto_mode`), `agent` (account of the assistant: `api:read` + `agent_vault`), `admin` (everything;
automatically for `INT_ADMIN`).

## Development

```bash
# Tests (in the dev core container, plugin as package ai_plugin)
tar --exclude=node_modules --exclude=.git --exclude=runner -cf - . | docker exec -i lyndrix-core-dev sh -c 'rm -rf /tmp/t-ai && mkdir -p /tmp/t-ai/ai_plugin && tar xf - -C /tmp/t-ai/ai_plugin'
docker exec -w /app -e PYTHONPATH=/app:/tmp/t-ai lyndrix-core-dev python -m pytest /tmp/t-ai/ai_plugin/tests -q
# React bundle
docker run --rm -u $(id -u):$(id -g) -v $PWD:/w -w /w node:20-slim sh -c 'npm install && npm run build'
# Runner image locally
docker build -t lyndrix-ai-runner:dev runner/
```

Limits of the first version: one workspace for all users (sessions separated, files
not); only provider `claude-code`; Hermes (for API-key models) follows as a second provider.
