# lyndrix-plugin-ai-assistant

Claude Code as an assistant inside Lyndrix: a chat in the shell (own tab and a floating,
pinnable panel), tool approvals like the Claude Code app, knowledge about the installed
plugins from their manifests, and its own Lyndrix account for API access.

- Claude Code runs in a sandboxed, long-lived container (`lyndrix-ai-runner`) on the
  Lyndrix instance with the `runner` role, driven through the Claude Agent SDK by a small
  HTTP bridge. The browser only talks to the plugin API.
- The workspace is a git repository on the shared storage; `CLAUDE.md` is generated from
  base instructions, one file per plugin that declares `ai_instructions`, and operator notes.
- Login with the Claude subscription happens from the settings page (the CLI's own OAuth
  flow, URL + code), or with an Anthropic API key.
- Guardrails: permission mode (`default` / `acceptEdits` / `plan` / `bypassPermissions`) plus
  allow / ask / deny rules per tool and command pattern.

German operator documentation: [documentation.md](documentation.md). Runner image:
[runner/README.md](runner/README.md). Requires Lyndrix core ≥ 1.9 (API 1.11: panels,
`ai_instructions`, `module_registry`).
