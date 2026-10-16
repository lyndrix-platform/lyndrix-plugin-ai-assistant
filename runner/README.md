# lyndrix-ai-runner

Long-lived Docker image that hosts **Claude Code** (`@anthropic-ai/claude-code`, pinned) plus a small
**HTTP bridge** (FastAPI + `claude-agent-sdk`). The Lyndrix AI-assistant plugin
(`lyndrix-plugin-ai-assistant`) starts one container per runner host and drives Claude Code sessions
through the bridge: JSON requests in, Server-Sent Events out, interactive tool approvals in between.

Image: `ghcr.io/lyndrix-platform/lyndrix-ai-runner:<VERSION>` (built from this directory by
`.github/workflows/runner-image.yml`; version in `VERSION`).

```
┌──────────────────────┐  HTTP + SSE   ┌─────────────────────────────────────────────┐
│ lyndrix-core         │ ─────────────▶│ lyndrix-ai-runner                           │
│  plugin ai-assistant │  X-Bridge-    │  bridge (uvicorn :8765)                     │
│  (RunnerManager,     │  Token        │   └─ claude_agent_sdk.ClaudeSDKClient       │
│   SSE proxy, UI)     │ ◀─────────────│        └─ claude (CLI, one process/session) │
└──────────────────────┘               │  /workspace  (bind mount, CLAUDE.md inside) │
                                       └─────────────────────────────────────────────┘
```

## How the plugin uses it

1. **Ensure the container** (`docker run -d --name lyndrix-ai-runner … ghcr.io/lyndrix-platform/lyndrix-ai-runner:0.1.0`)
   on the same Docker network as the core, with the workspace repo bind-mounted at `/workspace`.
2. **Health**: `GET /health` tells the plugin whether the CLI is present, which credential is active
   (`oauth` / `api_key` / `none`) and whether `LYNDRIX_API_URL`/`LYNDRIX_API_KEY` are available to the agent.
3. **Login** (no credential yet): `POST /auth/start` → show the URL to the user → `POST /auth/submit`
   with the pasted code → store the returned token in Vault → restart the runner with
   `CLAUDE_CODE_OAUTH_TOKEN` set.
4. **Chat**: `POST /sessions` per user conversation, `POST /sessions/{id}/messages` per prompt, follow
   `GET /sessions/{id}/events` (SSE), answer `permission_request` events via
   `POST /sessions/{id}/approvals/{approval_id}`.

Everything the agent should know about Lyndrix goes into `/workspace/CLAUDE.md` (the plugin generates
it); the bridge loads it through `setting_sources=["project"]`.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `LYNDRIX_AI_BRIDGE_TOKEN` | yes | Shared secret; every request must send it as `X-Bridge-Token` (401 otherwise, `/health` included). Without it the bridge rejects everything. |
| `CLAUDE_CODE_OAUTH_TOKEN` | one of | Long-lived subscription token from the login flow / `claude setup-token`. **Preferred.** |
| `ANTHROPIC_API_KEY` | one of | Pay-per-token API key. Never set both — Claude Code would bill the API key; `/health` reports `degraded` + a warning if both are present. |
| `LYNDRIX_AI_MODEL` | no | Default model for new sessions (a session may override it). |
| `LYNDRIX_API_URL`, `LYNDRIX_API_KEY` | no | Pass-through for the agent's own Lyndrix API access (used per CLAUDE.md). `/health` only reports `lyndrix_api: true`; the key is never printed. |
| `LYNDRIX_AI_WORKSPACE` | no | Default `cwd` for sessions (`/workspace`). |
| `LYNDRIX_AI_BRIDGE_PORT` | no | Listen port (`8765`). |
| `LYNDRIX_AI_APPROVAL_TIMEOUT_SECONDS` | no | Unanswered approvals are denied after this (`600`). |
| `LYNDRIX_AI_EVENT_BUFFER` | no | Events kept per session for `?after=` replay (`500`). |
| `LYNDRIX_AI_SETTING_SOURCES` | no | Claude Code setting sources (`project`). Add `user` to honour `~/.claude/settings.json`. |
| `LYNDRIX_AI_CLAUDE_BIN` | no | CLI path (`/usr/local/bin/claude`). |
| `LYNDRIX_AI_LOG_LEVEL` | no | `INFO`. Logs are JSON lines on stdout. |

Volumes: `/workspace` (the AI workspace repo; owned by whoever cloned it — `git safe.directory=*`
is preset) and `/home/agent/.claude` (Claude Code config + session transcripts; the plugin mounts the
named volume `lyndrix-ai-runner-home` there so `resume` works across container restarts). The image
has no `CMD`: the Lyndrix socket manager starts containers with an empty entrypoint, so the plugin
passes `/opt/bridge/entrypoint.sh` as the command.

The container runs as user `agent` (uid/gid `10000`, build args `AGENT_UID`/`AGENT_GID`) — gid 10000
is the group of the shared Lyndrix storage (`/data/storage`, setgid dirs), so files the agent writes get
the right group.

## HTTP contract

All routes require `X-Bridge-Token`. Errors are `{"detail": "...", "kind": "<ExceptionClass>"}`.

| Route | Purpose |
|---|---|
| `GET /health` | `{status: ok\|degraded, claude_version, sdk_version, bridge_version, provider, auth: oauth\|api_key\|none, auth_flow_pending, lyndrix_api, workspace, sessions, warnings[]}` |
| `POST /auth/start` | Starts `claude setup-token` → `{auth_id, url, expires_in, expires_at}`; 409 while one is pending; 502 if the CLI prints no URL |
| `POST /auth/submit` `{auth_id, code}` | `{ok: true, token}` **once** (never logged) or `{ok: false, error}`; either way the flow ends |
| `GET /auth/status` | `{pending, auth_id?, started_at?, expires_at?, url?, authenticated}` |
| `DELETE /auth/{auth_id}` | Cancels the pending flow |
| `POST /sessions` | Body below → `201 {session_id, created_at}` |
| `GET /sessions` | List of session summaries |
| `GET /sessions/{id}` | Summary + `events` (last 50) |
| `POST /sessions/{id}/messages` `{content}` | `202 {accepted: true}`; `409 TurnInProgress` while a turn runs; `409 SessionClosed` |
| `GET /sessions/{id}/events?after=<seq>` | SSE; replays stored events with `seq > after`, then live; `: keep-alive` every 15 s |
| `POST /sessions/{id}/approvals/{approval_id}` `{decision: allow\|deny\|allow_always, updated_input?, message?}` | Resolves a `permission_request`; 404 unknown/expired |
| `POST /sessions/{id}/interrupt` | `202`; 409 when no turn is running |
| `DELETE /sessions/{id}` | Closes the Claude Code process; status `closed`; the SSE stream ends |

### `POST /sessions` body

```json
{
  "session_id": "optional caller id (8–64 chars [A-Za-z0-9_-]); 409 SessionExists if taken",
  "user": "marvin",
  "title": "optional",
  "cwd": "/workspace",
  "permission_mode": "default | acceptEdits | bypassPermissions | plan",
  "allowed_tools": ["Read", "Grep"],
  "model": "optional model id",
  "append_system_prompt": "optional text appended to Claude Code's system prompt",
  "resume": "optional Claude session id to resume",
  "max_turns": 40,
  "policy": {
    "allow": [{"tool": "Bash", "match": "^git (status|diff|log)"}],
    "ask":   [{"tool": "*"}],
    "deny":  [{"tool": "Bash", "match": "rm -rf|sudo"}]
  }
}
```

`allowed_tools` is Claude Code's own pre-approval list (those calls never reach the bridge).
`policy` is evaluated by the bridge inside `can_use_tool`: **deny → allow → ask**; `match` is a regex
tested against a textual rendering of the input (Bash: the command; Edit/Write/Read: the file path;
Grep/Glob: pattern + path; WebFetch: URL; other/MCP tools: compact JSON). Unmatched calls fall back to
`permission_mode` (`default` → ask, `acceptEdits` → allow file edits / ask the rest, `bypassPermissions`
→ allow, `plan` → deny mutating tools / ask the rest). An approval answered with `allow_always` adds
`{tool, match: "^<exact rendered input>$"}` to the session's allow list; `GET /sessions/{id}` shows the
effective policy.

### Session summary

```json
{"session_id": "…", "user": "marvin", "title": null, "cwd": "/workspace", "created_at": "…",
 "last_activity": "…", "claude_session_id": "…", "status": "idle | running | waiting_approval | closed",
 "turns": 2, "total_cost_usd": 0.0312, "permission_mode": "default", "model": null,
 "allowed_tools": [], "policy": {...}, "pending_approvals": [], "last_seq": 42}
```

### Events

Each SSE frame is `id: <seq>` + `data: {"seq", "ts", "type", "data"}`.

| type | data |
|---|---|
| `status` | `{state: idle\|running\|waiting_approval\|closed}` |
| `assistant_text` | `{text, delta: bool}` — streamed deltas while Claude types; a whole block only when no deltas were streamed |
| `thinking` | `{text, delta: bool}` |
| `tool_use` | `{tool_use_id, name, input (≤ 2 kB, paths kept), parent_tool_use_id?}` |
| `tool_result` | `{tool_use_id, name, is_error, output (≤ 4 kB)}` |
| `permission_request` | `{approval_id, tool_use_id, name, input, rendered, suggestions[], title, display_name, description, blocked_path, decision_reason, timeout_s}` |
| `permission_resolved` | `{approval_id, decision: allow\|deny\|allow_always\|timeout, name}`; policy denials come with `approval_id: null, auto: true, by: policy\|permission_mode` |
| `result` | `{subtype, duration_ms, duration_api_ms, total_cost_usd, num_turns, claude_session_id, is_error, stop_reason, terminal_reason, result?}` |
| `error` | `{message, kind}` — SDK/CLI exceptions, Claude API errors (`assistant_error`), error results (`result_error`) |

A turn always ends with `result` (or `error`) followed by `status: idle`. After an error the Claude Code
process is dropped; the next message reopens it with `resume=<claude_session_id>` so context survives.

## Login flow (`claude setup-token`, verified with 2.1.301)

- The command **requires a TTY**: without one it prints a single newline and blocks forever. The bridge
  runs it on a pseudo-terminal 1000 columns wide so the URL is never line-wrapped.
- It prints "Welcome to Claude Code v2.1.301 … long-lived (1-year) auth token setup … Claude subscription
  required.", tries to open a browser, then "Browser didn't open? Use the url below to sign in (c to
  copy)" followed by `https://claude.com/cai/oauth/authorize?code=true&client_id=…&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Ainference&code_challenge=…&state=…`,
  and waits at "Paste code here if prompted >" (the code is read from stdin, terminated by Enter).
- A wrong code yields "OAuth error: Invalid code. Please make sure the full code was copied Press Enter
  to retry." and the process stays alive. The bridge treats any failure as the end of that flow (process
  terminated); the UI starts a new flow.
- On success the CLI prints the long-lived token (`sk-ant-oat01-…`) to use as `CLAUDE_CODE_OAUTH_TOKEN`.
  The bridge returns it once in the `/auth/submit` response and scrubs it from memory and logs.
  (The success output was not exercised against a live account during development — the token regex
  `sk-ant-oat01-[A-Za-z0-9_-]{20,}` follows the documented format; see "Open points".)
- Flows expire after 10 minutes (`expires_in`), one flow at a time.

## Running it manually

```bash
docker build -t lyndrix-ai-runner:dev .

mkdir -p /tmp/aiws && echo "# Test workspace" > /tmp/aiws/CLAUDE.md
docker run -d --name ai-runner \
  -e LYNDRIX_AI_BRIDGE_TOKEN=test \
  -e CLAUDE_CODE_OAUTH_TOKEN="$(cat ~/.lyndrix-ai-token)" \
  -v /tmp/aiws:/workspace -p 127.0.0.1:18765:8765 lyndrix-ai-runner:dev

H='X-Bridge-Token: test'
curl -s -H "$H" localhost:18765/health | jq
SID=$(curl -s -H "$H" -X POST localhost:18765/sessions -H 'content-type: application/json' \
      -d '{"user":"marvin","policy":{"allow":[{"tool":"Read"}]}}' | jq -r .session_id)
curl -s -H "$H" -N localhost:18765/sessions/$SID/events &      # follow the stream
curl -s -H "$H" -X POST localhost:18765/sessions/$SID/messages -H 'content-type: application/json' \
     -d '{"content":"List the files in the workspace and summarise CLAUDE.md"}'
# answer a permission_request:
curl -s -H "$H" -X POST localhost:18765/sessions/$SID/approvals/<approval_id> \
     -H 'content-type: application/json' -d '{"decision":"allow_always"}'
curl -s -H "$H" -X DELETE localhost:18765/sessions/$SID
docker rm -f ai-runner
```

Tests (no credentials needed; a fake provider and a fake `setup-token` stand-in are used):

```bash
docker run --rm --entrypoint /opt/bridge/venv/bin/python lyndrix-ai-runner:dev -m pytest /opt/bridge/bridge/tests -q
```

## Layout

```
runner/
├── Dockerfile            node:22-bookworm-slim + git/python3/ripgrep + claude-code@2.1.301 + venv
├── entrypoint.sh         prepare HOME (bridge.prepare_home) → python -m bridge (uvicorn)
├── VERSION               image version (workflow tags)
├── requirements*.txt     claude-agent-sdk==0.2.168, fastapi, uvicorn, pydantic (+ pytest, httpx)
└── bridge/
    ├── app.py            HTTP contract, auth dependency, SSE
    ├── auth.py           claude setup-token on a pty (login flow)
    ├── claude_provider.py  ClaudeSDKClient wiring + SDK message → event translation
    ├── sessions.py       session registry, background turns, approval futures
    ├── policy.py         allow/ask/deny guardrails
    ├── events.py         ring buffer + SSE fan-out, input/output summarisation
    ├── config.py, logging_setup.py, prepare_home.py, __main__.py
    └── tests/
```

The SDK wheel bundles its own copy of the CLI; the Dockerfile deletes it so the npm-pinned binary is the
only one (`cli_path` is set explicitly), which also saves ~270 MB.
