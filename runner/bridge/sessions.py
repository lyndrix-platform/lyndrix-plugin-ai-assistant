"""Session registry, turn runner and approval futures.

A *session* is one conversation of one Lyndrix user with Claude Code. The
provider session (the Claude Code process) is opened lazily on the first turn
and kept alive between turns so context persists; if it dies, the next turn
reopens it with ``resume=<claude_session_id>``.

Turns run as background tasks: ``POST /messages`` returns 202 immediately and
the caller follows the SSE stream. Only one turn per session may run at a time
(409 otherwise) because Claude Code processes one prompt at a time.

Approvals are ``asyncio.Future`` objects: the provider's ``can_use_tool``
callback parks on one, the HTTP layer resolves it, and a timeout turns into a
deny so a forgotten tab never wedges a session.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from .claude_provider import Decision, Provider, ProviderSession, SessionSpec, TranslationState, translate
from .config import Settings
from .events import EventLog, summarise_input, utc_now_iso
from .policy import Policy, fallback_verdict, render_input

log = logging.getLogger("bridge.sessions")

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_WAITING = "waiting_approval"
STATUS_CLOSED = "closed"

DECISIONS = ("allow", "deny", "allow_always")


class SessionError(Exception):
    """Base class; the HTTP layer maps subclasses to status codes."""


class SessionNotFound(SessionError):
    pass


class SessionExists(SessionError):
    pass


class SessionClosed(SessionError):
    pass


class TurnInProgress(SessionError):
    pass


class NoTurnRunning(SessionError):
    pass


class ApprovalNotFound(SessionError):
    pass


class ApprovalAlreadyResolved(SessionError):
    pass


@dataclass
class PendingApproval:
    approval_id: str
    tool_use_id: str | None
    name: str
    input: dict[str, Any]
    future: asyncio.Future[Decision]
    created_at: str = field(default_factory=utc_now_iso)


@dataclass
class Session:
    session_id: str
    spec: SessionSpec
    policy: Policy
    events: EventLog
    created_at: str = field(default_factory=utc_now_iso)
    last_activity: str = field(default_factory=utc_now_iso)
    status: str = STATUS_IDLE
    turns: int = 0
    total_cost_usd: float = 0.0
    claude_session_id: str | None = None
    pending: dict[str, PendingApproval] = field(default_factory=dict)
    tstate: TranslationState = field(default_factory=TranslationState)
    provider_session: ProviderSession | None = None
    turn_task: asyncio.Task[None] | None = None
    # Cost reported by the current Claude Code process is cumulative for that
    # process; remember the last reading to add only the delta per turn.
    _cost_reported: float = 0.0

    def touch(self) -> None:
        self.last_activity = utc_now_iso()

    def emit(self, type_: str, data: dict[str, Any] | None = None) -> None:
        self.events.append(type_, data)
        self.touch()

    def set_status(self, status: str) -> None:
        if self.status == STATUS_CLOSED and status != STATUS_CLOSED:
            return  # a closed session stays closed
        if self.status != status:
            self.status = status
            self.emit("status", {"state": status})

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user": self.spec.user,
            "title": self.spec.title,
            "cwd": self.spec.cwd,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "claude_session_id": self.claude_session_id,
            "status": self.status,
            "turns": self.turns,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "permission_mode": self.spec.permission_mode,
            "model": self.spec.model,
            "allowed_tools": list(self.spec.allowed_tools),
            "policy": self.policy.to_dict(),
            "pending_approvals": sorted(self.pending),
            "last_seq": self.events.last_seq,
        }

    def account_cost(self, reported: float | None) -> None:
        if reported is None:
            return
        delta = reported - self._cost_reported if reported >= self._cost_reported else reported
        self._cost_reported = reported
        self.total_cost_usd += max(0.0, delta)


class SessionManager:
    def __init__(self, provider: Provider, settings: Settings) -> None:
        self._provider = provider
        self._settings = settings
        self._sessions: dict[str, Session] = {}

    # ----------------------------------------------------------------- CRUD

    def create(self, spec: SessionSpec, policy: Policy | None = None, session_id: str | None = None) -> Session:
        """``session_id`` lets the plugin recreate a session it already stored
        under the same id after the bridge lost it (restart); 409 if it exists."""
        if session_id and session_id in self._sessions:
            raise SessionExists(session_id)
        session = Session(
            session_id=session_id or uuid.uuid4().hex,
            spec=spec,
            policy=policy or Policy(),
            events=EventLog(maxlen=self._settings.event_buffer),
            claude_session_id=spec.resume,
        )
        self._sessions[session.session_id] = session
        session.emit("status", {"state": STATUS_IDLE})
        log.info(
            "session created",
            extra={"ctx": {"session_id": session.session_id, "user": spec.user, "mode": spec.permission_mode}},
        )
        return session

    def list(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.created_at)

    def get(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise SessionNotFound(session_id) from None

    def __len__(self) -> int:
        return len(self._sessions)

    # ---------------------------------------------------------------- turns

    def start_turn(self, session: Session, content: str) -> asyncio.Task[None]:
        if session.status == STATUS_CLOSED:
            raise SessionClosed(session.session_id)
        if session.turn_task is not None and not session.turn_task.done():
            raise TurnInProgress(session.session_id)
        session.turn_task = asyncio.create_task(
            self._run_turn(session, content), name=f"turn:{session.session_id[:8]}"
        )
        return session.turn_task

    async def _ensure_provider_session(self, session: Session) -> ProviderSession:
        if session.provider_session is not None:
            return session.provider_session
        spec = session.spec
        if session.claude_session_id and spec.resume != session.claude_session_id:
            # Reopen where the previous process left off.
            spec = SessionSpec(**{**spec.__dict__, "resume": session.claude_session_id})
        session.provider_session = await self._provider.open(
            spec, approve=lambda name, inp, ctx: self._approve(session, name, inp, ctx)
        )
        session._cost_reported = 0.0
        return session.provider_session

    async def _drop_provider_session(self, session: Session) -> None:
        ps, session.provider_session = session.provider_session, None
        if ps is None:
            return
        try:
            await ps.close()
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("provider close failed", extra={"ctx": {"session_id": session.session_id, "error": str(exc)}})

    async def _run_turn(self, session: Session, content: str) -> None:
        session.set_status(STATUS_RUNNING)
        session.tstate.begin_turn()
        failed = False
        try:
            ps = await self._ensure_provider_session(session)
            async for message in ps.send(content):
                for type_, data in translate(message, session.tstate):
                    if type_ == "result":
                        session.account_cost(data.get("total_cost_usd"))
                        if data.get("is_error"):
                            failed = True
                    session.emit(type_, data)
                if session.tstate.claude_session_id:
                    session.claude_session_id = session.tstate.claude_session_id
            session.turns += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never let a turn kill the bridge
            failed = True
            log.exception("turn failed", extra={"ctx": {"session_id": session.session_id}})
            session.emit("error", {"message": str(exc) or exc.__class__.__name__, "kind": exc.__class__.__name__})
        finally:
            self._deny_pending(session, "turn ended")
            if failed:
                # The Claude Code process exits non-zero after an error result;
                # drop it so the next turn resumes in a fresh process.
                await self._drop_provider_session(session)
            session.set_status(STATUS_IDLE)

    async def interrupt(self, session: Session) -> None:
        if session.turn_task is None or session.turn_task.done() or session.provider_session is None:
            raise NoTurnRunning(session.session_id)
        self._deny_pending(session, "interrupted")
        await session.provider_session.interrupt()
        session.touch()

    async def close(self, session: Session) -> None:
        if session.status == STATUS_CLOSED:
            return
        self._deny_pending(session, "session closed")
        session.set_status(STATUS_CLOSED)
        await self._drop_provider_session(session)
        if session.turn_task is not None and not session.turn_task.done():
            session.turn_task.cancel()
            try:
                await asyncio.wait_for(session.turn_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        session.events.close()
        log.info("session closed", extra={"ctx": {"session_id": session.session_id}})

    async def shutdown(self) -> None:
        for session in list(self._sessions.values()):
            await self.close(session)

    # ------------------------------------------------------------ approvals

    def _deny_pending(self, session: Session, reason: str) -> None:
        for approval in list(session.pending.values()):
            if not approval.future.done():
                approval.future.set_result(Decision("deny", message=reason))

    async def _approve(
        self, session: Session, tool_name: str, tool_input: dict[str, Any], context: dict[str, Any]
    ) -> Decision:
        """Provider callback: policy first, then ask the human over SSE."""
        verdict = session.policy.evaluate(tool_name, tool_input)
        source = "policy"
        if verdict is None:
            verdict = fallback_verdict(session.spec.permission_mode, tool_name)
            source = "permission_mode"
        if verdict == "allow":
            log.info("tool auto-allowed", extra={"ctx": {"session_id": session.session_id, "tool": tool_name, "by": source}})
            return Decision("allow")
        if verdict == "deny":
            session.emit(
                "permission_resolved",
                {"approval_id": None, "decision": "deny", "name": tool_name, "auto": True, "by": source},
            )
            return Decision("deny", message=f"Blocked by session policy ({source})")

        approval = PendingApproval(
            approval_id=uuid.uuid4().hex[:12],
            tool_use_id=context.get("tool_use_id"),
            name=tool_name,
            input=summarise_input(tool_input),
            future=asyncio.get_running_loop().create_future(),
        )
        session.pending[approval.approval_id] = approval
        session.set_status(STATUS_WAITING)
        session.emit(
            "permission_request",
            {
                "approval_id": approval.approval_id,
                "tool_use_id": approval.tool_use_id,
                "name": tool_name,
                "input": approval.input,
                "rendered": render_input(tool_name, tool_input),
                "suggestions": context.get("suggestions") or [],
                "title": context.get("title"),
                "display_name": context.get("display_name"),
                "description": context.get("description"),
                "blocked_path": context.get("blocked_path"),
                "decision_reason": context.get("decision_reason"),
                "timeout_s": self._settings.approval_timeout_s,
            },
        )
        try:
            decision = await asyncio.wait_for(
                asyncio.shield(approval.future), timeout=self._settings.approval_timeout_s
            )
        except asyncio.TimeoutError:
            decision = Decision("timeout", message="Approval timed out")
        finally:
            session.pending.pop(approval.approval_id, None)
            if not session.pending and session.status == STATUS_WAITING:
                session.set_status(STATUS_RUNNING)

        if decision.decision == "allow_always":
            session.policy.allow_exact(tool_name, tool_input)
        session.emit(
            "permission_resolved",
            {"approval_id": approval.approval_id, "decision": decision.decision, "name": tool_name},
        )
        return decision

    def resolve_approval(
        self,
        session: Session,
        approval_id: str,
        decision: str,
        updated_input: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        approval = session.pending.get(approval_id)
        if approval is None:
            raise ApprovalNotFound(approval_id)
        if approval.future.done():
            raise ApprovalAlreadyResolved(approval_id)
        approval.future.set_result(Decision(decision, updated_input=updated_input, message=message))
        session.touch()
