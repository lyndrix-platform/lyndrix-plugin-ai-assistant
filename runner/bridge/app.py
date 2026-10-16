"""HTTP contract of the bridge (the plugin proxies these routes 1:1).

Auth: every route, ``/health`` included, requires ``X-Bridge-Token`` equal to
``LYNDRIX_AI_BRIDGE_TOKEN``. Before a Claude credential exists only ``/health``
and the ``/auth/*`` login flow are useful; sessions would fail with an ``error``
event, never crash the process.
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__
from .auth import AuthFlowError, AuthFlowManager, AuthFlowNotFound, AuthFlowNotReady, AuthFlowPending
from .claude_provider import PERMISSION_MODES, Provider, SessionSpec
from .config import Settings, detect_auth
from .policy import Policy, PolicyError
from .sessions import (
    ApprovalAlreadyResolved,
    ApprovalNotFound,
    NoTurnRunning,
    SessionClosed,
    SessionError,
    SessionExists,
    SessionManager,
    SessionNotFound,
    TurnInProgress,
)

log = logging.getLogger("bridge.app")

PermissionMode = Literal["default", "acceptEdits", "bypassPermissions", "plan"]
assert set(PermissionMode.__args__) == set(PERMISSION_MODES)  # type: ignore[attr-defined]


# ----------------------------------------------------------------------- bodies


class PolicyRuleIn(BaseModel):
    tool: str = Field(min_length=1)
    match: str | None = None


class PolicyIn(BaseModel):
    allow: list[PolicyRuleIn] = Field(default_factory=list)
    ask: list[PolicyRuleIn] = Field(default_factory=list)
    deny: list[PolicyRuleIn] = Field(default_factory=list)


class CreateSessionIn(BaseModel):
    # Optional: the caller's own id (the plugin stores sessions in its DB and
    # recreates them here after a bridge restart under the same id).
    session_id: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    user: str = Field(min_length=1)
    title: str | None = None
    cwd: str | None = None
    permission_mode: PermissionMode = "default"
    allowed_tools: list[str] = Field(default_factory=list)
    model: str | None = None
    append_system_prompt: str | None = None
    resume: str | None = None
    max_turns: int | None = Field(default=None, ge=1, le=1000)
    policy: PolicyIn | None = None


class MessageIn(BaseModel):
    content: str = Field(min_length=1)


class ApprovalIn(BaseModel):
    decision: Literal["allow", "deny", "allow_always"]
    updated_input: dict[str, Any] | None = None
    message: str | None = None


class AuthSubmitIn(BaseModel):
    auth_id: str = Field(min_length=1)
    code: str = Field(min_length=1)


class AuthResultIn(BaseModel):
    auth_id: str = Field(min_length=1)


# -------------------------------------------------------------------- factory


def create_app(
    settings: Settings | None = None,
    provider: Provider | None = None,
    auth_flows: AuthFlowManager | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if provider is None:
        from .claude_provider import ClaudeCodeProvider  # imports the SDK; tests inject a fake

        provider = ClaudeCodeProvider(settings)
    manager = SessionManager(provider, settings)
    auth_flows = auth_flows or AuthFlowManager(getattr(provider, "cli_path", None))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.claude_version = await provider.version()
        kind, warnings = detect_auth()
        log.info(
            "bridge ready",
            extra={
                "ctx": {
                    "bridge_version": __version__,
                    "claude_version": app.state.claude_version,
                    "sdk_version": getattr(provider, "sdk_version", None),
                    "auth": kind,
                    "workspace": settings.workspace,
                    "token_configured": bool(settings.bridge_token),
                    "warnings": warnings,
                }
            },
        )
        if not settings.bridge_token:
            log.error("LYNDRIX_AI_BRIDGE_TOKEN is not set; every request will be rejected with 401")
        try:
            yield
        finally:
            await manager.shutdown()
            await auth_flows.cancel()

    app = FastAPI(title="Lyndrix AI runner bridge", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.manager = manager
    app.state.provider = provider
    app.state.auth_flows = auth_flows
    app.state.claude_version = None

    async def require_token(x_bridge_token: str | None = Header(default=None, alias="X-Bridge-Token")) -> None:
        expected = settings.bridge_token
        if not expected or not x_bridge_token or not hmac.compare_digest(x_bridge_token.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="invalid or missing X-Bridge-Token")

    guarded = [Depends(require_token)]

    # ----------------------------------------------------------- exceptions

    status_for: dict[type[Exception], int] = {
        SessionNotFound: 404,
        ApprovalNotFound: 404,
        AuthFlowNotFound: 404,
        TurnInProgress: 409,
        SessionExists: 409,
        SessionClosed: 409,
        NoTurnRunning: 409,
        ApprovalAlreadyResolved: 409,
        AuthFlowPending: 409,
        AuthFlowNotReady: 409,
        PolicyError: 400,
    }

    @app.exception_handler(SessionError)
    @app.exception_handler(AuthFlowError)
    @app.exception_handler(PolicyError)
    async def _domain_error(_: Request, exc: Exception) -> JSONResponse:
        code = status_for.get(type(exc), 502 if isinstance(exc, AuthFlowError) else 500)
        return JSONResponse(status_code=code, content={"detail": str(exc) or exc.__class__.__name__, "kind": exc.__class__.__name__})

    # --------------------------------------------------------------- health

    @app.get("/health", dependencies=guarded)
    async def health() -> dict[str, Any]:
        if app.state.claude_version is None:
            app.state.claude_version = await provider.version()
        kind, warnings = detect_auth()
        return {
            "status": "ok" if app.state.claude_version and not warnings else "degraded",
            "claude_version": app.state.claude_version,
            "sdk_version": getattr(provider, "sdk_version", None),
            "bridge_version": __version__,
            "provider": provider.name,
            "auth": kind,
            "auth_flow_pending": auth_flows.pending is not None,
            "lyndrix_api": bool(os.environ.get("LYNDRIX_API_URL") and os.environ.get("LYNDRIX_API_KEY")),
            "workspace": settings.workspace,
            "sessions": len(manager),
            "warnings": warnings,
        }

    # ----------------------------------------------------------------- auth

    @app.post("/auth/start", dependencies=guarded)
    async def auth_start() -> dict[str, Any]:
        return await auth_flows.start()

    @app.post("/auth/submit", status_code=202, dependencies=guarded)
    async def auth_submit(body: AuthSubmitIn) -> dict[str, Any]:
        return await auth_flows.submit(body.auth_id, body.code)

    @app.post("/auth/result", dependencies=guarded)
    async def auth_result(body: AuthResultIn) -> dict[str, Any]:
        """Outcome of the exchange; the token is returned exactly once (409 while running)."""
        return await auth_flows.result(body.auth_id)

    @app.get("/auth/status", dependencies=guarded)
    async def auth_status() -> dict[str, Any]:
        return auth_flows.status()

    @app.delete("/auth/{auth_id}", dependencies=guarded)
    async def auth_cancel(auth_id: str) -> dict[str, Any]:
        if not await auth_flows.cancel(auth_id):
            raise AuthFlowNotFound(auth_id)
        return {"cancelled": True}

    # ------------------------------------------------------------- sessions

    @app.post("/sessions", status_code=201, dependencies=guarded)
    async def create_session(body: CreateSessionIn) -> dict[str, Any]:
        cwd = body.cwd or settings.workspace
        if not os.path.isdir(cwd):
            raise HTTPException(status_code=400, detail=f"cwd is not a directory: {cwd}")
        policy = Policy.from_dict(body.policy.model_dump() if body.policy else None)
        spec = SessionSpec(
            user=body.user,
            title=body.title,
            cwd=cwd,
            permission_mode=body.permission_mode,
            allowed_tools=list(body.allowed_tools),
            model=body.model,
            append_system_prompt=body.append_system_prompt,
            resume=body.resume,
            max_turns=body.max_turns,
        )
        session = manager.create(spec, policy, session_id=body.session_id)
        return {"session_id": session.session_id, "created_at": session.created_at}

    @app.get("/sessions", dependencies=guarded)
    async def list_sessions() -> list[dict[str, Any]]:
        return [session.to_dict() for session in manager.list()]

    @app.get("/sessions/{session_id}", dependencies=guarded)
    async def get_session(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        return {**session.to_dict(), "events": [event.to_dict() for event in session.events.last(50)]}

    @app.post("/sessions/{session_id}/messages", status_code=202, dependencies=guarded)
    async def post_message(session_id: str, body: MessageIn) -> dict[str, Any]:
        session = manager.get(session_id)
        manager.start_turn(session, body.content)
        return {"accepted": True, "session_id": session.session_id}

    @app.get("/sessions/{session_id}/events", dependencies=guarded)
    async def stream_events(request: Request, session_id: str, after: int = Query(default=0, ge=0)) -> StreamingResponse:
        session = manager.get(session_id)

        async def generate() -> AsyncIterator[str]:
            async for item in session.events.stream(after, keepalive=settings.keepalive_s):
                if item is None:
                    if await request.is_disconnected():
                        return
                    yield ": keep-alive\n\n"
                else:
                    yield item.to_sse()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.post("/sessions/{session_id}/approvals/{approval_id}", dependencies=guarded)
    async def resolve_approval(session_id: str, approval_id: str, body: ApprovalIn) -> dict[str, Any]:
        session = manager.get(session_id)
        manager.resolve_approval(session, approval_id, body.decision, body.updated_input, body.message)
        return {"resolved": True, "approval_id": approval_id, "decision": body.decision}

    @app.post("/sessions/{session_id}/interrupt", status_code=202, dependencies=guarded)
    async def interrupt(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        await manager.interrupt(session)
        return {"accepted": True}

    @app.delete("/sessions/{session_id}", dependencies=guarded)
    async def close_session(session_id: str) -> dict[str, Any]:
        session = manager.get(session_id)
        await manager.close(session)
        return {"session_id": session.session_id, "status": session.status}

    return app


# uvicorn entry point: ``bridge.app:app`` (see bridge/__main__.py)
app = create_app()
