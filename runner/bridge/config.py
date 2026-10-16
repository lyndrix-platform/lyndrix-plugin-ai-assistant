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
    # "project" picks up /workspace/CLAUDE.md and /workspace/.claude/settings.json;
    # "user" would add the agent's own ~/.claude/settings.json.
    setting_sources: list[str] = field(default_factory=lambda: ["project"])

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
            setting_sources=_split_csv(env.get("LYNDRIX_AI_SETTING_SOURCES"), ["project"]),
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
