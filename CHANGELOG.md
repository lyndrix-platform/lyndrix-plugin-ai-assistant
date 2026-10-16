# Changelog

All notable changes to this plugin are documented here.

## [0.1.0] - 2026-10-16
### Added
- Claude Code as an in-app assistant: a long-lived sandboxed runner container (`lyndrix-ai-runner`) driven by an HTTP bridge built on the Claude Agent SDK, started on the instance with the `runner` role (placement `pinned:runner`, lease `ai_assistant:runner`).
- Chat as a sidebar tab (`/ai/chat`) and as a floating, per-user pinnable panel (core API 1.11 panels); settings/status page (`/ai/settings`).
- Login with the Claude subscription through the CLI's own OAuth flow from the settings page (URL + code), or with an Anthropic API key.
- Workspace: git repository on the shared storage (optional remote), `CLAUDE.md` generated from base instructions, every loaded plugin's `ai_instructions` and operator notes.
- Guardrails like the Claude Code app: permission mode plus allow / ask / deny rules per tool and pattern; interactive approvals in the chat.
- The assistant's own Lyndrix account (`ai-assistant`, bot) with an API key passed into the runner; allowed Vault spaces readable through the plugin API with audit logging.
- Sessions and transcripts in the database (`ai_sessions`, `ai_messages`).
