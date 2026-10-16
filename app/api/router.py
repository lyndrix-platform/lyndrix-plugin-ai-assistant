"""REST API of the AI Assistant plugin — mounted at
``/api/plugins/lyndrix.plugin.ai_assistant/`` by the core (auth at the registry
level). The browser never talks to the runner container directly: chat
sessions, event streams and approvals are proxied through here, scoped to the
calling user. All blocking work (DB, git, docker) is offloaded.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.api import ApiIdentity, require_permission

from ..logic.bridge import BridgeError
from ..logic.service import PERMISSION_MODES, ai_service

PLUGIN_ID = "lyndrix.plugin.ai_assistant"
PERM_READ = f"plugin:{PLUGIN_ID}:api:read"
PERM_WRITE = f"plugin:{PLUGIN_ID}:api:write"
PERM_ADMIN = f"plugin:{PLUGIN_ID}:api:admin"


class SessionCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    permission_mode: Optional[str] = None


class MessageIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=50_000)


class PermissionModeIn(BaseModel):
    permission_mode: str = Field(..., pattern="^(default|acceptEdits|plan|auto|bypassPermissions)$")


class ApprovalIn(BaseModel):
    decision: str = Field(..., pattern="^(allow|deny|allow_always)$")
    updated_input: Optional[dict] = None
    message: Optional[str] = Field(default=None, max_length=500)


class AuthSubmit(BaseModel):
    auth_id: str
    code: str = Field(..., min_length=4, max_length=400)


def _bridge_http(exc: BridgeError) -> HTTPException:
    status = exc.status if exc.status in (400, 404, 409, 422, 503, 504) else 502
    headers = {"Retry-After": "10"} if status == 503 else None
    return HTTPException(status_code=status, detail=exc.detail, headers=headers)


def _owned(session_id: str, identity: ApiIdentity) -> dict:
    """The session, 404 unless it belongs to the caller (admins see every session)."""
    row = ai_service.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if row["owner"] != identity.username and not identity.allows(PERM_ADMIN):
        raise HTTPException(status_code=404, detail="Session not found")
    return row


def _require_runner() -> None:
    if ai_service.runner is None or ai_service.runner.client is None:
        raise HTTPException(
            status_code=503,
            detail="The assistant runner is not available on this instance.",
            headers={"Retry-After": "10"},
        )


def build_router() -> APIRouter:
    router = APIRouter()

    # ------------------------------------------------------------------ status

    @router.get("/status")
    async def status(_identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        return await ai_service.status()

    @router.get("/instructions")
    async def instructions(_identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        text = await asyncio.to_thread(ai_service.workspace.read_instructions) if ai_service.workspace else ""
        return {"markdown": text}

    @router.get("/ai/instructions")
    async def ai_instructions(_identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        """This plugin's own full AI guide (core API 1.12 contract: served live from the repo file)."""
        from pathlib import Path

        from ..logic.service import PLUGIN_VERSION

        path = Path(__file__).resolve().parents[2] / "ai" / "INSTRUCTIONS.md"
        text = await asyncio.to_thread(lambda: path.read_text(encoding="utf-8") if path.is_file() else "")
        return {"plugin_id": PLUGIN_ID, "version": PLUGIN_VERSION, "format": "markdown", "markdown": text}

    @router.post("/workspace/sync")
    async def workspace_sync(_identity: ApiIdentity = Depends(require_permission(PERM_ADMIN))):
        await asyncio.to_thread(ai_service.reload_settings)
        return await ai_service.sync_workspace()

    @router.post("/runner/restart")
    async def runner_restart(_identity: ApiIdentity = Depends(require_permission(PERM_ADMIN))):
        await asyncio.to_thread(ai_service.reload_settings)
        health = await ai_service.ensure_runner(force=True)
        return {"runner": health}

    @router.get("/runner/logs")
    async def runner_logs(tail: int = 200, _identity: ApiIdentity = Depends(require_permission(PERM_ADMIN))):
        if ai_service.runner is None:
            raise HTTPException(status_code=503, detail="runner not bound")
        return {"logs": await ai_service.runner.logs(tail=max(10, min(tail, 2000)))}

    # ------------------------------------------------------------------ Claude login (OAuth via the CLI)

    @router.post("/auth/start")
    async def auth_start(_identity: ApiIdentity = Depends(require_permission(PERM_ADMIN))):
        _require_runner()
        try:
            # the CLI needs a moment before it prints the URL (bridge waits up to 45 s)
            return await ai_service.bridge()._call("POST", "/auth/start", timeout=75)
        except BridgeError as exc:
            raise _bridge_http(exc) from exc

    @router.post("/auth/submit", status_code=202)
    async def auth_submit(payload: AuthSubmit, identity: ApiIdentity = Depends(require_permission(PERM_ADMIN))):
        """Hands the code to the CLI and returns at once; the exchange (up to 90 s)
        is followed server-side and the token stored — poll ``/auth/status``
        (``last_result``) instead of keeping one long request open through the proxies."""
        _require_runner()
        try:
            await ai_service.bridge()._call(
                "POST", "/auth/submit", json={"auth_id": payload.auth_id, "code": payload.code.strip()}, timeout=30,
            )
        except BridgeError as exc:
            raise _bridge_http(exc) from exc
        ai_service.start_login_finisher(payload.auth_id, by=identity.username)
        return {"accepted": True, "auth_id": payload.auth_id}

    @router.post("/auth/verify")
    async def auth_verify(_identity: ApiIdentity = Depends(require_permission(PERM_ADMIN))):
        """Real check of the configured credential: one tiny headless turn in the runner."""
        _require_runner()
        try:
            return await ai_service.bridge()._call("POST", "/auth/verify", timeout=100)
        except BridgeError as exc:
            raise _bridge_http(exc) from exc

    @router.delete("/auth/{auth_id}")
    async def auth_cancel(auth_id: str, _identity: ApiIdentity = Depends(require_permission(PERM_ADMIN))):
        """Abort a pending login (the CLI process is terminated; a new one can start)."""
        _require_runner()
        try:
            return await ai_service.bridge()._call("DELETE", f"/auth/{auth_id}")
        except BridgeError as exc:
            raise _bridge_http(exc) from exc

    @router.get("/auth/status")
    async def auth_status(_identity: ApiIdentity = Depends(require_permission(PERM_ADMIN))):
        if ai_service.runner is None or ai_service.runner.client is None:
            return {"pending": False, "authenticated": ai_service.auth_mode(), "runner": "down",
                    "last_result": ai_service.login_last}
        try:
            data = await ai_service.bridge()._call("GET", "/auth/status")
        except BridgeError as exc:
            # the runner is being recreated with the fresh token right after a login
            if ai_service.login_last and ai_service.login_last.get("phase") in ("storing", "done"):
                return {"pending": False, "authenticated": ai_service.auth_mode(), "runner": "restarting",
                        "last_result": ai_service.login_last}
            raise _bridge_http(exc) from exc
        data["last_result"] = ai_service.login_last
        return data

    # ------------------------------------------------------------------ sessions

    @router.get("/sessions")
    async def list_sessions(all: bool = False, archived: bool = False,
                            identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        owner = None if (all and identity.allows(PERM_ADMIN)) else identity.username
        return {"sessions": await asyncio.to_thread(ai_service.list_sessions, owner, archived)}

    @router.post("/sessions", status_code=201)
    async def create_session(payload: SessionCreate, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))):
        _require_runner()
        if payload.permission_mode and payload.permission_mode not in PERMISSION_MODES:
            raise HTTPException(status_code=422, detail=f"permission_mode must be one of {PERMISSION_MODES}")
        try:
            return await ai_service.create_session(identity.username, payload.title, payload.permission_mode)
        except BridgeError as exc:
            raise _bridge_http(exc) from exc

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str, identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        row = await asyncio.to_thread(_owned, session_id, identity)
        messages = await asyncio.to_thread(ai_service.messages, session_id)
        live: dict[str, Any] = {}
        if ai_service.runner is not None and ai_service.runner.client is not None:
            try:
                live = await ai_service.bridge().get_session(session_id)
            except BridgeError:
                live = {}
        return {"session": row, "messages": messages, "live": live}

    @router.post("/sessions/{session_id}/messages", status_code=202)
    async def send_message(session_id: str, payload: MessageIn,
                           identity: ApiIdentity = Depends(require_permission(PERM_WRITE))):
        _require_runner()
        await asyncio.to_thread(_owned, session_id, identity)
        try:
            return await ai_service.send_message(session_id, identity.username, payload.content)
        except BridgeError as exc:
            raise _bridge_http(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/sessions/{session_id}/events")
    async def session_events(session_id: str, request: Request, after: int = 0,
                             identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        """SSE proxy of the runner's event stream for one session."""
        _require_runner()
        await asyncio.to_thread(_owned, session_id, identity)
        bridge = ai_service.bridge()

        async def _gen():
            try:
                async for chunk in bridge.stream_events(session_id, after=after):
                    if await request.is_disconnected():
                        break
                    yield chunk
            except BridgeError as exc:
                yield f'data: {{"seq": 0, "type": "error", "data": {{"message": "{exc.detail}"}}}}\n\n'.encode()
            except Exception as exc:  # noqa: BLE001 — the client sees a clean end instead of a 500
                yield f'data: {{"seq": 0, "type": "error", "data": {{"message": "{str(exc)[:200]}"}}}}\n\n'.encode()

        return StreamingResponse(
            _gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/sessions/{session_id}/approvals/{approval_id}")
    async def approve(session_id: str, approval_id: str, payload: ApprovalIn,
                      identity: ApiIdentity = Depends(require_permission(PERM_WRITE))):
        _require_runner()
        await asyncio.to_thread(_owned, session_id, identity)
        try:
            return await ai_service.approve(session_id, approval_id, payload.decision, payload.updated_input, payload.message)
        except BridgeError as exc:
            raise _bridge_http(exc) from exc

    @router.post("/sessions/{session_id}/permission-mode")
    async def set_permission_mode(session_id: str, payload: PermissionModeIn,
                                  identity: ApiIdentity = Depends(require_permission(PERM_WRITE))):
        """Per-session approval switch. ``auto`` allows everything the guardrails do not
        deny/ask; ``bypassPermissions`` also disables the guardrails and is admin-only."""
        _require_runner()
        await asyncio.to_thread(_owned, session_id, identity)
        if payload.permission_mode == "bypassPermissions" and not identity.allows(PERM_ADMIN):
            raise HTTPException(status_code=403, detail="bypassPermissions disables the guardrails — administrators only")
        try:
            return await ai_service.set_permission_mode(session_id, identity.username, payload.permission_mode)
        except BridgeError as exc:
            raise _bridge_http(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/sessions/{session_id}/interrupt")
    async def interrupt(session_id: str, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))):
        _require_runner()
        await asyncio.to_thread(_owned, session_id, identity)
        try:
            return await ai_service.interrupt(session_id)
        except BridgeError as exc:
            raise _bridge_http(exc) from exc

    @router.delete("/sessions/{session_id}")
    async def close_session(session_id: str, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))):
        await asyncio.to_thread(_owned, session_id, identity)
        try:
            await ai_service.close_session(session_id)
        except BridgeError as exc:
            raise _bridge_http(exc) from exc
        return {"ok": True}

    # ------------------------------------------------------------------ Vault spaces for the agent

    @router.get("/vault/spaces")
    async def vault_spaces(identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        """Spaces the operator allowed (setting ``vault_spaces``) — names only."""
        return {"spaces": ai_service.allowed_vault_spaces()}

    @router.get("/vault/spaces/{space}")
    async def vault_space(space: str, identity: ApiIdentity = Depends(require_permission(PERM_ADMIN))):
        """Values of one allowed space. Read by the agent with its own Lyndrix
        credentials (``PERM_ADMIN`` granted to the assistant user), audited."""
        try:
            data = await asyncio.to_thread(ai_service.read_vault_space, space)
        except KeyError:
            raise HTTPException(status_code=404, detail="Space not allowed or unknown")
        ai_service.log_access(identity.username, f"vault space '{space}'")
        return {"space": space, "values": data}

    return router
