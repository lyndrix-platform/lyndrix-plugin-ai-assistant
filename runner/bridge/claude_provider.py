"""Claude Code provider: the only module that speaks ``claude_agent_sdk``.

Two responsibilities, kept apart so each is testable on its own:

* :func:`translate` — a pure function turning SDK message objects into bridge
  events (``assistant_text``, ``tool_use``, ``result`` …). Tests feed it fake
  SDK objects; the live session feeds it whatever ``receive_response`` yields.
* :class:`ClaudeCodeProvider` — opens a long-lived ``ClaudeSDKClient`` per
  bridge session (so conversation context survives between turns), wires the
  ``can_use_tool`` callback, and maps bridge decisions back to the SDK's
  ``PermissionResultAllow`` / ``PermissionResultDeny``.

Verified against claude-agent-sdk 0.2.168: ``can_use_tool`` only works in
streaming mode (``ClaudeSDKClient``); the SDK rewrites it to
``--permission-prompt-tool stdio`` and runs the callback in its own task, so
the message stream keeps flowing while a human decides.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import warnings
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk import __version__ as SDK_VERSION

from .config import Settings
from .events import MAX_OUTPUT_BYTES, summarise_input, tool_result_text, truncate_text

log = logging.getLogger("bridge.claude")

# Operators may pre-approve whole tools via allowed_tools; the SDK warns that
# the callback is then skipped for them. That is intended here, so log instead.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

PERMISSION_MODES = ("default", "acceptEdits", "bypassPermissions", "plan")


# --------------------------------------------------------------------------- #
# Provider-neutral contract (sessions.py depends only on this part)
# --------------------------------------------------------------------------- #


@dataclass
class SessionSpec:
    """What the plugin asked for when it created the session."""

    user: str
    cwd: str
    title: str | None = None
    permission_mode: str = "default"
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    append_system_prompt: str | None = None
    resume: str | None = None
    max_turns: int | None = None


@dataclass
class Decision:
    """The bridge's answer to a permission request."""

    decision: str  # allow | allow_always | deny | timeout
    updated_input: dict[str, Any] | None = None
    message: str | None = None


# (tool_name, tool_input, context) -> Decision ; context is a plain dict
ApprovalCallback = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[Decision]]


class ProviderSession(Protocol):
    async def send(self, content: str) -> AsyncIterator[Any]: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...


class Provider(Protocol):
    name: str

    async def open(self, spec: SessionSpec, *, approve: ApprovalCallback) -> ProviderSession: ...
    async def version(self) -> str | None: ...


# --------------------------------------------------------------------------- #
# Message translation
# --------------------------------------------------------------------------- #


@dataclass
class TranslationState:
    """Carried across the messages of one session (tool names outlive turns)."""

    tool_names: dict[str, str] = field(default_factory=dict)
    claude_session_id: str | None = None
    text_streamed: bool = False
    thinking_streamed: bool = False
    error_emitted: bool = False

    def begin_turn(self) -> None:
        self.text_streamed = False
        self.thinking_streamed = False
        self.error_emitted = False


def result_error_text(message: ResultMessage) -> str:
    """Best error prose for an ``is_error`` result (mirrors the SDK's own pick order)."""
    if message.errors:
        return "; ".join(str(err) for err in message.errors)
    if isinstance(message.result, str) and message.result.strip():
        return message.result.strip()
    if message.subtype and message.subtype != "success":
        return message.subtype
    if message.api_error_status is not None:
        return f"API error (HTTP {message.api_error_status})"
    return "unknown error"


def _translate_stream_event(message: StreamEvent, state: TranslationState) -> list[tuple[str, dict]]:
    if message.parent_tool_use_id:  # sub-agent chatter; the parent tool_result carries the summary
        return []
    event = message.event or {}
    if event.get("type") != "content_block_delta":
        return []
    delta = event.get("delta") or {}
    kind = delta.get("type")
    if kind == "text_delta" and delta.get("text"):
        state.text_streamed = True
        return [("assistant_text", {"text": delta["text"], "delta": True})]
    if kind == "thinking_delta" and delta.get("thinking"):
        state.thinking_streamed = True
        return [("thinking", {"text": delta["thinking"], "delta": True})]
    return []


def _translate_assistant(message: AssistantMessage, state: TranslationState) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            if not state.text_streamed and block.text:
                out.append(("assistant_text", {"text": block.text, "delta": False}))
        elif isinstance(block, ThinkingBlock):
            if not state.thinking_streamed and block.thinking:
                out.append(("thinking", {"text": block.thinking, "delta": False}))
        elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
            state.tool_names[block.id] = block.name
            data: dict[str, Any] = {
                "tool_use_id": block.id,
                "name": block.name,
                "input": summarise_input(block.input),
            }
            if message.parent_tool_use_id:
                data["parent_tool_use_id"] = message.parent_tool_use_id
            out.append(("tool_use", data))
    # A complete assistant message closes the streamed-text window: the next
    # message starts fresh so its whole-text fallback works again.
    state.text_streamed = False
    state.thinking_streamed = False
    if message.error:
        state.error_emitted = True
        out.append(("error", {"message": f"Claude API error: {message.error}", "kind": "assistant_error"}))
    return out


def _translate_user(message: UserMessage, state: TranslationState) -> list[tuple[str, dict]]:
    if not isinstance(message.content, list):
        return []  # echo of a user prompt
    out: list[tuple[str, dict]] = []
    for block in message.content:
        if isinstance(block, ToolResultBlock):
            out.append(
                (
                    "tool_result",
                    {
                        "tool_use_id": block.tool_use_id,
                        "name": state.tool_names.get(block.tool_use_id, "unknown"),
                        "is_error": bool(block.is_error),
                        "output": truncate_text(tool_result_text(block.content), MAX_OUTPUT_BYTES),
                    },
                )
            )
        elif isinstance(block, ServerToolResultBlock):
            out.append(
                (
                    "tool_result",
                    {
                        "tool_use_id": block.tool_use_id,
                        "name": state.tool_names.get(block.tool_use_id, "server_tool"),
                        "is_error": False,
                        "output": truncate_text(tool_result_text(block.content), MAX_OUTPUT_BYTES),
                    },
                )
            )
    return out


def _translate_result(message: ResultMessage, state: TranslationState) -> list[tuple[str, dict]]:
    state.claude_session_id = message.session_id or state.claude_session_id
    data: dict[str, Any] = {
        "subtype": message.subtype,
        "duration_ms": message.duration_ms,
        "duration_api_ms": message.duration_api_ms,
        "total_cost_usd": message.total_cost_usd,
        "num_turns": message.num_turns,
        "claude_session_id": message.session_id,
        "is_error": bool(message.is_error),
        "stop_reason": message.stop_reason,
        "terminal_reason": message.terminal_reason,
    }
    if isinstance(message.result, str) and message.result:
        data["result"] = truncate_text(message.result, MAX_OUTPUT_BYTES)
    out: list[tuple[str, dict]] = [("result", data)]
    if message.is_error and not state.error_emitted:
        state.error_emitted = True
        out.append(("error", {"message": result_error_text(message), "kind": "result_error"}))
    return out


def translate(message: Any, state: TranslationState) -> list[tuple[str, dict]]:
    """Map one SDK message to zero or more ``(event_type, data)`` pairs."""
    if isinstance(message, StreamEvent):
        return _translate_stream_event(message, state)
    if isinstance(message, AssistantMessage):
        return _translate_assistant(message, state)
    if isinstance(message, UserMessage):
        return _translate_user(message, state)
    if isinstance(message, ResultMessage):
        return _translate_result(message, state)
    if isinstance(message, SystemMessage):
        if message.subtype == "init":
            state.claude_session_id = message.data.get("session_id") or state.claude_session_id
        return []
    return []


# --------------------------------------------------------------------------- #
# SDK wiring
# --------------------------------------------------------------------------- #


def context_to_dict(context: ToolPermissionContext) -> dict[str, Any]:
    return {
        "tool_use_id": context.tool_use_id,
        "agent_id": context.agent_id,
        "blocked_path": context.blocked_path,
        "decision_reason": context.decision_reason,
        "title": context.title,
        "display_name": context.display_name,
        "description": context.description,
        "suggestions": [suggestion.to_dict() for suggestion in context.suggestions],
    }


def to_permission_result(decision: Decision) -> PermissionResultAllow | PermissionResultDeny:
    """Bridge decision → SDK result. ``allow_always`` is remembered by the bridge policy."""
    if decision.decision in ("allow", "allow_always"):
        return PermissionResultAllow(updated_input=decision.updated_input)
    if decision.decision == "timeout":
        return PermissionResultDeny(message=decision.message or "Approval timed out")
    return PermissionResultDeny(message=decision.message or "Denied by user")


def build_options(
    spec: SessionSpec,
    settings: Settings,
    *,
    can_use_tool: Any,
    cli_path: str | None,
    stderr: Callable[[str], None] | None = None,
) -> ClaudeAgentOptions:
    """Translate a session spec into SDK options.

    ``system_prompt`` must be the ``claude_code`` preset explicitly — the SDK
    sends an *empty* system prompt when the option is left ``None``.
    """
    system_prompt: dict[str, Any] = {"type": "preset", "preset": "claude_code"}
    if spec.append_system_prompt:
        system_prompt["append"] = spec.append_system_prompt
    return ClaudeAgentOptions(
        cwd=spec.cwd,
        permission_mode=spec.permission_mode,  # type: ignore[arg-type]
        allowed_tools=list(spec.allowed_tools),
        model=spec.model or settings.model,
        resume=spec.resume,
        max_turns=spec.max_turns,
        system_prompt=system_prompt,  # type: ignore[arg-type]
        setting_sources=list(settings.setting_sources),  # type: ignore[arg-type]
        can_use_tool=can_use_tool,
        include_partial_messages=True,
        cli_path=cli_path,
        # NOT options.user: in 0.2.168 the transport forwards it as a subprocess
        # kwarg ("unexpected kwargs: user"); the bridge tracks the user itself.
        stderr=stderr,
    )


class ClaudeCodeSession:
    """One live ``ClaudeSDKClient`` (= one Claude Code process)."""

    def __init__(self, client: ClaudeSDKClient) -> None:
        self._client = client

    async def send(self, content: str) -> AsyncIterator[Any]:
        await self._client.query(content)
        async for message in self._client.receive_response():
            yield message

    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def close(self) -> None:
        await self._client.disconnect()


class ClaudeCodeProvider:
    name = "claude-code"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.cli_path = settings.claude_bin or shutil.which("claude")
        self.sdk_version = SDK_VERSION

    async def version(self) -> str | None:
        """``claude --version`` → "2.1.301"; ``None`` when the CLI is unusable."""
        if not self.cli_path:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_path,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except Exception as exc:  # noqa: BLE001 - reported via health
            log.warning("claude --version failed", extra={"ctx": {"error": str(exc)}})
            return None
        match = re.search(r"(\d+\.\d+\.\d+)", out.decode(errors="ignore"))
        return match.group(1) if match else None

    async def open(self, spec: SessionSpec, *, approve: ApprovalCallback) -> ClaudeCodeSession:
        async def can_use_tool(
            tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
        ) -> PermissionResultAllow | PermissionResultDeny:
            decision = await approve(tool_name, tool_input, context_to_dict(context))
            return to_permission_result(decision)

        def on_stderr(line: str) -> None:
            log.info("claude stderr", extra={"ctx": {"user": spec.user, "line": line[:500]}})

        options = build_options(
            spec, self._settings, can_use_tool=can_use_tool, cli_path=self.cli_path, stderr=on_stderr
        )
        client = ClaudeSDKClient(options)
        await client.connect()
        return ClaudeCodeSession(client)
