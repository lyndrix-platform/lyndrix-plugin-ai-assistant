"""Bridge settings, read once from the environment.

Everything the container needs to know arrives as environment variables set by
the plugin's RunnerManager (``docker run -e ...``). Keeping the parsing in one
place lets tests build a :class:`Settings` by hand instead of mutating
``os.environ``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from .hardening import AGENT_ENV, CREDENTIAL_ENV

DEFAULT_WORKSPACE = "/workspace"
DEFAULT_PORT = 8765
DEFAULT_APPROVAL_TIMEOUT_S = 600.0
DEFAULT_EVENT_BUFFER = 500
DEFAULT_KEEPALIVE_S = 15.0

AuthKind = str  # "oauth" | "api_key" | "none"


def _split_csv(value: str | None, default: list[str]) -> list[str]:
    if value is None or not value.strip():
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Runtime configuration of the bridge process."""

    bridge_token: str | None = None
    workspace: str = DEFAULT_WORKSPACE
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    model: str | None = None
    approval_timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S
    event_buffer: int = DEFAULT_EVENT_BUFFER
    keepalive_s: float = DEFAULT_KEEPALIVE_S
    claude_bin: str | None = None
    # Filesystem settings the CLI loads. Default: none (AI-04). "project" would load
    # /workspace/CLAUDE.md and /workspace/.claude/settings.json — the workspace is shared
    # by every user and writable by the agent, so one session could plant hooks, allow
    # rules or instructions for everybody else's sessions. The plugin sends the
    # instructions as append_system_prompt instead. Opt back in with
    # LYNDRIX_AI_SETTING_SOURCES=project (and/or user) only for a single-user setup.
    setting_sources: list[str] = field(default_factory=list)
    # Secrets, read once from the environment and then removed from it
    # (bridge.hardening.scrub_environ): the Claude credential and the agent's own
    # Lyndrix account. Handed to each CLI explicitly (cli_env), never inherited.
    credential_env: dict[str, str] = field(default_factory=dict)
    agent_env: dict[str, str] = field(default_factory=dict)
    # Refuse requests from inside the container (the agent's network namespace).
    reject_local_peers: bool = True

    def cli_env(self) -> dict[str, str]:
        """Environment a Claude Code process gets on top of the scrubbed ``os.environ``
        — everything it needs, and never the bridge token."""
        return {**self.credential_env, **self.agent_env}

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            bridge_token=env.get("LYNDRIX_AI_BRIDGE_TOKEN") or None,
            workspace=env.get("LYNDRIX_AI_WORKSPACE", DEFAULT_WORKSPACE),
            host=env.get("LYNDRIX_AI_BRIDGE_HOST", "0.0.0.0"),
            port=int(env.get("LYNDRIX_AI_BRIDGE_PORT", DEFAULT_PORT)),
            model=env.get("LYNDRIX_AI_MODEL") or None,
            approval_timeout_s=float(
                env.get("LYNDRIX_AI_APPROVAL_TIMEOUT_SECONDS", DEFAULT_APPROVAL_TIMEOUT_S)
            ),
            event_buffer=int(env.get("LYNDRIX_AI_EVENT_BUFFER", DEFAULT_EVENT_BUFFER)),
            keepalive_s=float(env.get("LYNDRIX_AI_SSE_KEEPALIVE_SECONDS", DEFAULT_KEEPALIVE_S)),
            claude_bin=env.get("LYNDRIX_AI_CLAUDE_BIN") or None,
            setting_sources=_split_csv(env.get("LYNDRIX_AI_SETTING_SOURCES"), []),
            credential_env={k: env[k] for k in CREDENTIAL_ENV if env.get(k)},
            agent_env={k: env[k] for k in AGENT_ENV if env.get(k)},
            reject_local_peers=str(env.get("LYNDRIX_AI_BRIDGE_ALLOW_LOCAL", "")).strip().lower() not in ("1", "true", "yes"),
        )


def detect_auth(env: Mapping[str, str] | None = None) -> tuple[AuthKind, list[str]]:
    """Report which Claude credential is present without ever revealing it.

    Claude Code prefers ``ANTHROPIC_API_KEY`` over the subscription token when
    both are set, which silently moves usage to pay-per-token billing — the
    design doc forbids that, so having both is reported as a warning.
    """
    env = os.environ if env is None else env
    has_oauth = bool(env.get("CLAUDE_CODE_OAUTH_TOKEN"))
    has_api_key = bool(env.get("ANTHROPIC_API_KEY"))
    warnings: list[str] = []
    if has_oauth and has_api_key:
        warnings.append(
            "Both CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_API_KEY are set; "
            "Claude Code will bill the API key. Set only one."
        )
    if has_oauth:
        return "oauth", warnings
    if has_api_key:
        return "api_key", warnings
    return "none", warnings
