# KI-Assistent (lyndrix-plugin-ai-assistant) — AI operating instructions

## Purpose
This plugin is *you*: it runs Claude Code in the sandboxed `lyndrix-ai-runner` container, proxies the
chat (sessions, SSE events, tool approvals) through its API and keeps the transcript in the database.
Operators reach for it to ask questions about the platform, to let you inspect plugins through their
APIs and to let you work in this git workspace.

## What you can call
- `GET /api/plugins/lyndrix.plugin.ai_assistant/status` — runner health, auth mode, workspace state.
- `GET /api/plugins/lyndrix.plugin.ai_assistant/vault/spaces` — the Vault spaces the operator allowed
  for you; `GET …/vault/spaces/<space>` returns their values (audited). Never echo values into the chat.
- `GET /api/ai/plugins` — the live plugin index (purpose + guide URL per plugin).
- `GET /api/me/access` — your own effective permissions.

## Guardrails
- Tool approvals are decided by the operator's policy (allow / ask / deny rules, permission mode);
  an "ask" shows up as an approval card in the chat — wait for it, never work around a deny.
- Sessions, transcripts and approvals are per user; do not assume you see other users' chats.
- The workspace is the only writable place; `.lyndrix/cache/` holds fetched plugin guides and may be
  deleted at any time.
