"""Shared fixtures: a scriptable fake provider so no test needs Claude credentials.

The fake yields *real* ``claude_agent_sdk`` message objects, so the translation
code under test is the same one the live provider feeds.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # runner/ → "import bridge"

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from bridge.claude_provider import ApprovalCallback, Decision, SessionSpec  # noqa: E402
from bridge.config import Settings  # noqa: E402
from bridge.sessions import SessionManager  # noqa: E402

TOKEN = "test-token"


# --------------------------------------------------------------- SDK helpers


def assistant(*blocks: Any, model: str = "claude-test", message_id: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model=model, message_id=message_id)


def text(value: str) -> TextBlock:
    return TextBlock(text=value)


def tool_use(tool_id: str, name: str, **tool_input: Any) -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name=name, input=tool_input)


def tool_result(tool_id: str, content: Any, is_error: bool | None = None) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool_id, content=content, is_error=is_error)])


def result(
    session_id: str = "claude-session-1",
    cost: float | None = 0.01,
    is_error: bool = False,
    subtype: str = "success",
    errors: list[str] | None = None,
    result_text: str | None = None,
    num_turns: int = 1,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=120,
        duration_api_ms=100,
        is_error=is_error,
        num_turns=num_turns,
        session_id=session_id,
        total_cost_usd=cost,
        errors=errors,
        result=result_text,
    )


# -------------------------------------------------------------- fake provider


@dataclass
class Ask:
    """Script step: call the approval callback like the SDK would."""

    tool: str
    input: dict[str, Any]
    tool_use_id: str = "toolu_ask"


@dataclass
class Gate:
    """Script step: block until released (lets tests observe a running turn)."""

    event: asyncio.Event = field(default_factory=asyncio.Event)

    def release(self) -> None:
        self.event.set()


@dataclass
class Raise:
    exc: Exception


Script = list[Any] | Callable[[str], list[Any]]


class FakeProviderSession:
    def __init__(self, provider: "FakeProvider", spec: SessionSpec, approve: ApprovalCallback) -> None:
        self.provider = provider
        self.spec = spec
        self.approve = approve
        self.closed = False

    async def send(self, content: str) -> AsyncIterator[Any]:
        self.provider.sent.append(content)
        for step in self.provider.script_for(content):
            if isinstance(step, Gate):
                await step.event.wait()
            elif isinstance(step, Ask):
                decision = await self.approve(step.tool, step.input, {"tool_use_id": step.tool_use_id, "suggestions": []})
                self.provider.decisions.append(decision)
            elif isinstance(step, Raise):
                raise step.exc
            else:
                yield step

    async def interrupt(self) -> None:
        self.provider.interrupted += 1
        for step in self.provider.script_for(""):
            if isinstance(step, Gate):
                step.release()

    async def close(self) -> None:
        self.closed = True
        self.provider.closed += 1


class FakeProvider:
    name = "fake"
    sdk_version = "0.0-fake"
    cli_path = None

    def __init__(self, script: Script | None = None) -> None:
        self.script: Script = script if script is not None else [assistant(text("hello")), result()]
        self.opened: list[SessionSpec] = []
        self.sent: list[str] = []
        self.decisions: list[Decision] = []
        self.interrupted = 0
        self.closed = 0

    def script_for(self, content: str) -> list[Any]:
        return self.script(content) if callable(self.script) else self.script

    async def open(self, spec: SessionSpec, *, approve: ApprovalCallback) -> FakeProviderSession:
        self.opened.append(spec)
        return FakeProviderSession(self, spec, approve)

    async def version(self) -> str | None:
        return "2.1.301"


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(bridge_token=TOKEN, workspace=str(tmp_path), approval_timeout_s=5.0, keepalive_s=0.2, event_buffer=50)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def manager(provider: FakeProvider, settings: Settings) -> SessionManager:
    return SessionManager(provider, settings)


@pytest.fixture
def spec(settings: Settings) -> SessionSpec:
    return SessionSpec(user="marvin", cwd=settings.workspace, title="t")


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-Bridge-Token": TOKEN}


async def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)
