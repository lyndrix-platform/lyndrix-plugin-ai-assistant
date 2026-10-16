"""Lyndrix AI Assistant — Claude Code in a sandboxed container, chat in the shell.

Manifest + lifecycle wiring only (business logic lives in ``app/logic``). The
plugin is pinned to the ``runner`` role: that instance has the Docker socket
(core socket manager) and starts the long-lived ``lyndrix-ai-runner`` container
with the shared workspace mounted. One instance drives the container (lease
``ai_assistant:runner``); every runner-role instance serves the API.
"""
from __future__ import annotations

import asyncio
import time

from core.api import ModuleManifest, PluginHealthStatus, db_instance

from .app.api.router import build_router
from .app.logic.service import ai_service
from .app.logic.socket_client import SocketBusClient

PLUGIN_ID = "lyndrix.plugin.ai_assistant"

manifest = ModuleManifest(
    id=PLUGIN_ID,
    name="KI-Assistent",
    version="0.1.4",
    description="Claude Code als Assistent in Lyndrix: Chat, Werkzeug-Freigaben, Plugin-Wissen.",
    author="Lyndrix",
    icon="smart_toy",
    type="PLUGIN",
    min_core_version="1.11.0",   # panels, ai_instructions, module_registry
    auto_enable_on_install=False,       # needs a Claude login first
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-ai-assistant",
    placement="pinned:runner",
    react_ui=True,
    i18n_namespace="ai_assistant",
    react_routes=[
        {"path": "/ai/chat", "label": "KI-Assistent", "icon": "smart_toy", "sidebar_visible": True},
        {"path": "/ai/settings", "label": "KI-Assistent – Einstellungen", "icon": "settings", "sidebar_visible": False},
    ],
    settings_ui_route="/ai/settings",
    # Core API 1.11: floating, per-user pinnable window hosting the chat.
    panels=[{
        "id": "chat", "label": "KI-Chat", "icon": "smart_toy",
        "description": "Chat mit dem KI-Assistenten als schwebendes Fenster.",
        "default_size": {"width": 440, "height": 620},
    }],
    ai_instructions=(
        "You are talking to the operator through the AI Assistant plugin. Your own Lyndrix "
        "identity is the user `ai-assistant`; `LYNDRIX_API_URL` and `LYNDRIX_API_KEY` in your "
        "environment let you call the Lyndrix REST API (bearer key) with exactly the permissions "
        "the operator granted that user — try `GET /api/me/access` to see them. Allowed Vault "
        "spaces are listed at `GET /api/plugins/lyndrix.plugin.ai_assistant/vault/spaces` and read "
        "at `/vault/spaces/<space>`; never echo their values back into the chat."
    ),
    permissions={
        "subscribe": ["db:connected", "vault:ready_for_data", "system:boot_complete",
                      "plugin:settings_changed", "plugin:state_changed", "socket:response"],
        "emit": ["socket:request"],
    },
    custom_permissions=[
        {"id": "api:admin", "label": "Assistant administration",
         "description": "Runner, workspace, Claude login and Vault-space access."},
    ],
    roles=[
        {"id": "viewer", "label": "AI Assistant Viewer",
         "permissions": ["plugin:lyndrix.plugin.ai_assistant:api:read"],
         "auto_map_groups": [], "description": "Read chat sessions."},
        {"id": "user", "label": "AI Assistant User",
         "permissions": ["plugin:lyndrix.plugin.ai_assistant:api:read", "plugin:lyndrix.plugin.ai_assistant:api:write"],
         "auto_map_groups": [], "description": "Chat with the assistant and approve its tool calls."},
        {"id": "agent", "label": "AI Assistant Agent",
         "permissions": ["plugin:lyndrix.plugin.ai_assistant:api:read", "plugin:lyndrix.plugin.ai_assistant:api:admin"],
         "auto_map_groups": [], "description": "The assistant's own account: read status and allowed Vault spaces."},
        {"id": "admin", "label": "AI Assistant Administrator",
         "permissions": ["plugin:lyndrix.plugin.ai_assistant:api:read", "plugin:lyndrix.plugin.ai_assistant:api:write",
                         "plugin:lyndrix.plugin.ai_assistant:api:admin"],
         "auto_map_groups": ["INT_ADMIN"], "description": "Full control: login, runner, workspace, guardrails."},
    ],
    settings_schema=[
        {"key": "provider", "label": "Provider", "kind": "select", "options": ["claude-code"], "default": "claude-code",
         "category": "Provider", "description": "LLM backend. Claude Code runs in the sandboxed runner container; Hermes follows later."},
        {"key": "claude_auth_mode", "label": "Claude authentication", "kind": "select",
         "options": ["oauth_token", "api_key"], "default": "oauth_token", "category": "Provider",
         "description": "oauth_token = Claude subscription (log in on the settings page, or paste a token from `claude setup-token`); api_key = Anthropic API key (billed per token)."},
        {"key": "claude_oauth_token", "label": "Claude OAuth token", "kind": "secret", "category": "Provider",
         "description": "Filled automatically by the login flow on the settings page; may also be pasted from `claude setup-token`."},
        {"key": "anthropic_api_key", "label": "Anthropic API key", "kind": "secret", "category": "Provider",
         "description": "Only when authentication is api_key. Never set together with a subscription token."},
        {"key": "model", "label": "Model", "kind": "str", "category": "Provider",
         "description": "Optional model override (e.g. claude-sonnet-4-5). Empty = Claude Code default."},
        {"key": "runner_image", "label": "Runner image", "kind": "str", "default": "ghcr.io/lyndrix-platform/lyndrix-ai-runner:latest",
         "category": "Runner", "description": "Container image with Claude Code + bridge. Changing it recreates the runner."},
        {"key": "agent_lyndrix_access", "label": "Agent gets its own Lyndrix account", "kind": "bool", "default": "true",
         "category": "Runner", "description": "Creates the user `ai-assistant` (bot) with an API key and passes LYNDRIX_API_URL/KEY into the runner. Grant it roles in the Users page."},
        {"key": "agent_api_url", "label": "Lyndrix API URL for the agent", "kind": "str", "category": "Runner",
         "description": "How the runner reaches this instance. Empty = http://<this container>:8081 on the shared Docker network."},
        {"key": "workspace_repo_url", "label": "Workspace repository", "kind": "str", "category": "Workspace",
         "description": "Optional git remote (https) for the workspace. Empty = local repository on the shared storage."},
        {"key": "workspace_branch", "label": "Workspace branch", "kind": "str", "default": "main", "category": "Workspace"},
        {"key": "workspace_git_token", "label": "Git token", "kind": "secret", "category": "Workspace",
         "description": "Token for a private workspace remote; used per git command, never stored in the repository."},
        {"key": "extra_instructions", "label": "Additional instructions", "kind": "str", "category": "Workspace",
         "description": "Operator notes appended to the generated CLAUDE.md (Markdown; use ';' for line breaks)."},
        {"key": "permission_mode", "label": "Tool approval mode", "kind": "select",
         "options": ["default", "acceptEdits", "plan", "bypassPermissions"], "default": "default", "category": "Guardrails",
         "description": "Like the Claude Code app: default asks for every tool; acceptEdits allows file edits in the workspace; plan never mutates; bypassPermissions allows everything (sandbox only)."},
        {"key": "guardrails_allow", "label": "Always allow", "kind": "str", "category": "Guardrails",
         "description": "Rules 'Tool' or 'Tool: regex' separated by ';' that run without asking, e.g. 'Read; Grep; Glob; Bash: ^git (status|log|diff)'."},
        {"key": "guardrails_ask", "label": "Always ask", "kind": "str", "category": "Guardrails",
         "description": "Rules that always need an approval in the chat even in acceptEdits/bypass mode, e.g. 'Bash: ^git push'."},
        {"key": "guardrails_deny", "label": "Always deny", "kind": "str", "category": "Guardrails",
         "description": "Rules that are refused without asking, e.g. 'Bash: rm -rf; Bash: curl .*\\| *sh'."},
        {"key": "vault_spaces", "label": "Vault spaces for the agent", "kind": "str", "category": "Guardrails",
         "description": "Comma-separated KV spaces below the lyndrix mount the agent may read via the plugin API, e.g. 'plugins/lyndrix.plugin.docker'. Empty = none."},
        {"key": "max_turns", "label": "Max agent turns per message", "kind": "int", "default": "40", "category": "Guardrails"},
        {"key": "sync_workspace", "label": "Sync workspace & instructions", "kind": "action",
         "action": "/workspace/sync", "method": "POST", "category": "Workspace"},
        {"key": "restart_runner", "label": "Restart runner", "kind": "action", "action": "/runner/restart",
         "method": "POST", "confirm": True, "danger": True, "category": "Runner"},
    ],
)

plugin_state: dict = {"ready": False}


def _spawn(ctx, coro, name: str) -> None:
    create_task = getattr(ctx, "create_task", None)
    if callable(create_task):
        create_task(coro, name=name)
    else:
        asyncio.get_running_loop().create_task(coro)


def setup(ctx):
    ctx.log.info("KI-Assistent: starting setup...")
    ai_service.bind(ctx, SocketBusClient(ctx))
    ctx.register_routes(build_router())

    async def _bootstrap():
        await ai_service.bootstrap()
        plugin_state["ready"] = True

    if db_instance.is_connected:
        _spawn(ctx, _bootstrap(), "ai_assistant:bootstrap")

    @ctx.subscribe("db:connected")
    async def _on_db(payload=None):
        await _bootstrap()

    @ctx.subscribe("vault:ready_for_data")
    async def _on_vault(payload=None):
        await asyncio.to_thread(ai_service.reload_settings)

    # Exactly one runner-role instance drives the container; the others serve
    # the API against the same container by name.
    on_lease = getattr(ctx, "on_lease", None)
    if callable(on_lease):
        on_lease("ai_assistant:runner", acquired=ai_service.start_runner_role, lost=ai_service.stop_runner_role)
    else:
        _spawn(ctx, ai_service.start_runner_role(), "ai_assistant:runner")

    @ctx.subscribe("plugin:settings_changed")
    async def _on_settings(payload=None):
        if (payload or {}).get("plugin_id") != manifest.id:
            return
        await asyncio.to_thread(ai_service.reload_settings)
        await ai_service.refresh_instructions()
        if ai_service.is_runner_host:
            await ai_service.ensure_runner()   # recreated only when image/credentials changed

    @ctx.subscribe("plugin:state_changed")
    async def _on_plugins_changed(payload=None):
        # Another plugin was installed/removed/reloaded: its instructions may have changed.
        if (payload or {}).get("plugin_id") == manifest.id:
            return
        await ai_service.refresh_instructions()

    ctx.log.info("KI-Assistent: setup complete.")


async def health(ctx) -> PluginHealthStatus:
    start = time.perf_counter()
    if not plugin_state.get("ready"):
        return PluginHealthStatus(status="degraded", details={"reason": "bootstrapping"})
    runner = await ai_service.runner_health()
    latency = round((time.perf_counter() - start) * 1000, 1)
    details = {
        "runner": runner.get("status"),
        "auth": ai_service.auth_mode(),
        "runner_host": ai_service.is_runner_host,
        "sessions": runner.get("sessions"),
    }
    if ai_service.auth_mode() == "none":
        return PluginHealthStatus(status="degraded", details={**details, "reason": "claude_not_logged_in"}, latency_ms=latency)
    if ai_service.is_runner_host and runner.get("status") not in ("ok", "degraded"):
        return PluginHealthStatus(status="degraded", details={**details, "reason": "runner_unavailable"}, latency_ms=latency)
    return PluginHealthStatus(status="ok", details=details, latency_ms=latency)


def teardown(ctx):
    plugin_state["ready"] = False
    ctx.log.info("KI-Assistent: teardown complete (runner container keeps running).")
