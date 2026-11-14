"""Minimal stand-in for ``claude_agent_sdk`` — used ONLY when the real SDK is not
installed (plain host checkouts, the plugin CI). ``conftest.py`` registers it under
the SDK's module name before any bridge module imports it; inside the runner image
the real package is present and this file is never loaded.

It mirrors just the names and keyword fields the bridge touches. Objects accept
keyword arguments only and answer ``None`` for fields a test did not set, like the
SDK's dataclasses with their defaults. It deliberately implements no behaviour: what
the CLI does with options, hooks and permission results is verified against the real
SDK in the image build, not here.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.0.0-stub"


class _Record:
    _defaults: dict[str, Any] = {}

    def __init__(self, *args: Any, **fields: Any) -> None:
        if args:
            raise TypeError("the SDK stub takes keyword arguments only")
        for key, value in {**self._defaults, **fields}.items():
            setattr(self, key, value)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return None

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and self.__dict__ == other.__dict__

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.__dict__})"


def _record(name: str, **defaults: Any) -> type:
    return type(name, (_Record,), {"_defaults": defaults})


AssistantMessage = _record("AssistantMessage", content=[])
ResultMessage = _record("ResultMessage", errors=None)
ServerToolResultBlock = _record("ServerToolResultBlock")
ServerToolUseBlock = _record("ServerToolUseBlock")
StreamEvent = _record("StreamEvent")
SystemMessage = _record("SystemMessage", data={})
TextBlock = _record("TextBlock")
ThinkingBlock = _record("ThinkingBlock")
ToolPermissionContext = _record("ToolPermissionContext", suggestions=[])
ToolResultBlock = _record("ToolResultBlock")
ToolUseBlock = _record("ToolUseBlock")
UserMessage = _record("UserMessage")
PermissionResultAllow = _record("PermissionResultAllow", behavior="allow")
PermissionResultDeny = _record("PermissionResultDeny", behavior="deny")
ClaudeAgentOptions = _record("ClaudeAgentOptions")
HookMatcher = _record("HookMatcher", matcher=None, hooks=[])


class CanUseToolShadowedWarning(Warning):
    pass


class ClaudeSDKClient:
    def __init__(self, options: Any = None) -> None:
        self.options = options
