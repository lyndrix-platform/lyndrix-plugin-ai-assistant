"""AI Assistant service singleton.

Owns the workspace, the runner container, the session records and the
transcript recorder. Everything blocking (git, docker, DB) is offloaded;
``setup()`` only wires and schedules.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select

from core.api import get_logger

from ..model.database import db_ready, ensure_tables, get_session, retry_write
from ..model.models import AiMessage, AiSession
from .access import ensure_assistant_user, issue_api_key, parse_vault_spaces, read_vault_space
from .bridge import BridgeClient, BridgeError
from .runner import RunnerConfig, RunnerManager
from .workspace import WorkspaceManager

log = get_logger("Plugin:AI:Service")

PROVIDERS = ("claude-code",)           # "hermes" follows once the Hermes provider exists
PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions")
DEFAULT_IMAGE = "ghcr.io/lyndrix-platform/lyndrix-ai-runner:latest"
TRANSCRIPT_MAX_CHARS = 6000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AiAssistantService:
    def __init__(self) -> None:
        self.ctx = None
        self.storage_dir = Path("/data/storage")
        self.workspace: Optional[WorkspaceManager] = None
        self.runner: Optional[RunnerManager] = None
        self.settings: dict[str, Any] = {}
        self._tables_ready = False
        self._bootstrap_lock = asyncio.Lock()
        self._recorders: dict[str, asyncio.Task] = {}
        self._seqs: dict[str, int] = {}          # last bridge event seq seen per session
        self._health_cache: tuple[float, dict] | None = None
        self.is_runner_host = False      # set by the lease: this instance drives the container

    # ------------------------------------------------------------------ wiring

    def bind(self, ctx, socket_client) -> None:
        self.ctx = ctx
        try:
            from config import settings as core_settings  # TODO(agent): expose STORAGE_DIR via core.api

            self.storage_dir = Path(getattr(core_settings, "STORAGE_DIR", "/data/storage"))
        except Exception:
            pass
        self.workspace = WorkspaceManager(self.storage_dir)
        self.runner = RunnerManager(socket_client, self.storage_dir)

    @property
    def is_ready(self) -> bool:
        return self._tables_ready and db_ready()

    # ------------------------------------------------------------------ settings

    def reload_settings(self) -> dict:
        """Blocking: read the schema-driven settings (Vault)."""
        getter = getattr(self.ctx, "get_all_settings", None)
        values = {}
        if callable(getter):
            try:
                values = getter() or {}
            except Exception as exc:  # noqa: BLE001
                log.warning(f"Settings unreadable — {exc}")
        self.settings = values
        return values

    def setting(self, key: str, default: Any = None) -> Any:
        val = self.settings.get(key)
        return default if val in (None, "") else val

    def auth_env(self) -> dict[str, str]:
        """Exactly one credential for the runner (OAuth token wins when both exist)."""
        mode = str(self.setting("claude_auth_mode", "oauth_token"))
        token = self.setting("claude_oauth_token")
        key = self.setting("anthropic_api_key")
        if mode == "api_key" and key:
            return {"ANTHROPIC_API_KEY": str(key)}
        if token:
            return {"CLAUDE_CODE_OAUTH_TOKEN": str(token)}
        if key:
            return {"ANTHROPIC_API_KEY": str(key)}
        return {}

    def auth_mode(self) -> str:
        env = self.auth_env()
        return "oauth" if "CLAUDE_CODE_OAUTH_TOKEN" in env else "api_key" if env else "none"

    # ------------------------------------------------------------------ bootstrap

    async def bootstrap(self) -> None:
        """Tables, settings, workspace — on every instance that hosts the plugin."""
        async with self._bootstrap_lock:
            if db_ready() and not self._tables_ready:
                try:
                    await asyncio.to_thread(ensure_tables)
                    self._tables_ready = True
                except Exception as exc:  # noqa: BLE001
                    log.error(f"Table bootstrap failed — {exc}")
            await asyncio.to_thread(self.reload_settings)
            await self.sync_workspace()

    async def sync_workspace(self) -> dict:
        if self.workspace is None:
            return {"ok": False, "error": "not bound"}
        result = await asyncio.to_thread(
            self.workspace.sync,
            repo_url=self.setting("workspace_repo_url"),
            branch=str(self.setting("workspace_branch", "main")),
            token=self.setting("workspace_git_token"),
            extra_instructions=self.setting("extra_instructions"),
        )
        return result

    async def refresh_instructions(self) -> dict:
        if self.workspace is None:
            return {"ok": False}
        return await asyncio.to_thread(self.workspace.regenerate_instructions, self.setting("extra_instructions"))

    # ------------------------------------------------------------------ runner

    async def _bridge_token(self) -> str:
        """Shared secret between plugin and bridge, kept in Vault across restarts."""
        get_secret = getattr(self.ctx, "get_secret", None)
        set_secret = getattr(self.ctx, "set_secret", None)
        token = None
        if callable(get_secret):
            try:
                token = await asyncio.to_thread(get_secret, "bridge_token")
            except Exception:  # noqa: BLE001
                token = None
        if not token:
            token = RunnerManager.new_bridge_token()
            if callable(set_secret):
                try:
                    await asyncio.to_thread(set_secret, "bridge_token", token)
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"Could not persist the bridge token in Vault — {exc}")
        return str(token)

    async def runner_config(self) -> RunnerConfig:
        host_storage = await self.runner.host_storage_dir()  # type: ignore[union-attr]
        auth_env = dict(self.auth_env())
        auth_env.update(await self._agent_access_env())
        return RunnerConfig(
            image=str(self.setting("runner_image", DEFAULT_IMAGE)),
            auth_env=auth_env,
            bridge_token=await self._bridge_token(),
            model=self.setting("model"),
            host_workspace=f"{host_storage.rstrip('/')}/ai_workspace/repo",
            network=await asyncio.to_thread(RunnerManager.own_network),
        )

    async def _agent_access_env(self) -> dict[str, str]:
        """``LYNDRIX_API_URL`` / ``LYNDRIX_API_KEY`` for the agent's own Lyndrix
        identity (setting ``agent_lyndrix_access``, default on)."""
        if not self._truthy(self.setting("agent_lyndrix_access", True)) or not self.is_ready:
            return {}
        get_secret = getattr(self.ctx, "get_secret", None)
        set_secret = getattr(self.ctx, "set_secret", None)
        try:
            await asyncio.to_thread(ensure_assistant_user)
            key = await asyncio.to_thread(get_secret, "assistant_api_key") if callable(get_secret) else None
            if not key:
                key = await asyncio.to_thread(issue_api_key)
                if callable(set_secret):
                    await asyncio.to_thread(set_secret, "assistant_api_key", key)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Assistant account/API key not available — {exc}")
            return {}
        import socket

        url = str(self.setting("agent_api_url") or f"http://{socket.gethostname()}:8081")
        return {"LYNDRIX_API_URL": url, "LYNDRIX_API_KEY": str(key)}

    @staticmethod
    def _truthy(value: Any) -> bool:
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def guardrail_policy(self) -> dict:
        """Settings ``guardrails_allow`` / ``guardrails_ask`` / ``guardrails_deny``:
        one rule per line, ``Tool`` or ``Tool: <regex>`` (regex against the
        command / path of the tool call). Evaluated by the bridge: deny → allow → ask."""
        policy: dict[str, list] = {"allow": [], "ask": [], "deny": []}
        for key in policy:
            raw = str(self.setting(f"guardrails_{key}") or "")
            for line in raw.replace(";", "\n").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                tool, _, match = line.partition(":")
                rule = {"tool": tool.strip() or "*"}
                if match.strip():
                    rule["match"] = match.strip()
                policy[key].append(rule)
        return policy

    async def store_oauth_token(self, token: str, *, by: str) -> None:
        """Login through the UI succeeded: keep the token (Vault settings) and
        bring the runner up with it."""
        from core.components.vault.logic.kv_helper import read_secret_dict, write_secret_dict  # TODO(agent): core.api surface

        path = self.ctx._get_settings_vault_path()  # type: ignore[union-attr]
        data = await asyncio.to_thread(read_secret_dict, path) or {}
        data["claude_oauth_token"] = token
        data["claude_auth_mode"] = "oauth_token"
        await asyncio.to_thread(write_secret_dict, path, data)
        log.info(f"Claude OAuth token stored by '{by}' — restarting the runner with it.")
        await asyncio.to_thread(self.reload_settings)
        await self.ensure_runner(force=True)

    def allowed_vault_spaces(self) -> list[str]:
        return parse_vault_spaces(self.setting("vault_spaces"))

    def read_vault_space(self, space: str) -> dict:
        return read_vault_space(space, self.allowed_vault_spaces())

    def log_access(self, who: str, what: str) -> None:
        log.info(f"ACCESS: '{who}' read {what}")

    async def ensure_runner(self, *, force: bool = False) -> dict:
        """Lease holder only: make the container match the settings."""
        if self.runner is None:
            return {"status": "not_bound"}
        if not self.auth_env():
            self.runner.last_error = "no Claude credential configured (claude_oauth_token or anthropic_api_key)"
            return {"status": "unconfigured", "error": self.runner.last_error}
        cfg = await self.runner_config()
        health = await self.runner.ensure(cfg, force=force)
        self._health_cache = None
        return health

    async def start_runner_role(self) -> None:
        """``on_lease`` acquired: this instance drives the container."""
        self.is_runner_host = True
        await self.ensure_runner()

    async def stop_runner_role(self) -> None:
        """``on_lease`` lost: stop driving (the container keeps running for the peer)."""
        self.is_runner_host = False

    async def runner_health(self, max_age_s: float = 15.0) -> dict:
        import time

        if self._health_cache and time.monotonic() - self._health_cache[0] < max_age_s:
            return self._health_cache[1]
        health = await self.runner.health() if self.runner else {"status": "not_bound"}
        self._health_cache = (time.monotonic(), health)
        return health

    def bridge(self) -> BridgeClient:
        if self.runner is None or self.runner.client is None:
            raise BridgeError(503, "runner not started on this instance")
        return self.runner.client

    # ------------------------------------------------------------------ status

    async def status(self) -> dict:
        ws = await asyncio.to_thread(self.workspace.status) if self.workspace else {}
        return {
            "provider": str(self.setting("provider", "claude-code")),
            "auth_mode": self.auth_mode(),
            "runner": await self.runner_health(),
            "runner_host": self.is_runner_host,
            "runner_error": getattr(self.runner, "last_error", None),
            "workspace": ws,
            "db_ready": self.is_ready,
            "permission_mode": str(self.setting("permission_mode", "default")),
            "model": self.setting("model"),
        }

    # ------------------------------------------------------------------ sessions

    def _row_to_dict(self, row: AiSession) -> dict:
        return {
            "id": row.id, "owner": row.owner, "title": row.title, "provider": row.provider, "model": row.model,
            "permission_mode": row.permission_mode, "claude_session_id": row.claude_session_id,
            "status": row.status, "turns": row.turns, "total_cost_usd": row.total_cost_usd,
            "archived": row.archived,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_activity": row.last_activity.isoformat() if row.last_activity else None,
        }

    def list_sessions(self, owner: Optional[str], include_archived: bool = False) -> list[dict]:
        if not self.is_ready:
            return []
        with get_session() as s:
            q = select(AiSession).order_by(AiSession.last_activity.desc())
            if owner:
                q = q.where(AiSession.owner == owner)
            if not include_archived:
                q = q.where(AiSession.archived.is_(False))
            return [self._row_to_dict(r) for r in s.scalars(q).all()]

    def get_session(self, session_id: str) -> Optional[dict]:
        if not self.is_ready:
            return None
        with get_session() as s:
            row = s.get(AiSession, session_id)
            return self._row_to_dict(row) if row else None

    def messages(self, session_id: str, limit: int = 200) -> list[dict]:
        if not self.is_ready:
            return []
        with get_session() as s:
            rows = s.scalars(
                select(AiMessage).where(AiMessage.session_id == session_id)
                .order_by(AiMessage.id.desc()).limit(limit)
            ).all()
        return [
            {"id": r.id, "role": r.role, "kind": r.kind, "content": r.content, "meta": r.meta,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in reversed(rows)
        ]

    async def create_session(self, owner: str, title: Optional[str] = None,
                             permission_mode: Optional[str] = None) -> dict:
        mode = permission_mode or str(self.setting("permission_mode", "default"))
        if mode not in PERMISSION_MODES:
            mode = "default"
        payload = {
            "user": owner, "title": title or "Neue Unterhaltung", "cwd": "/workspace",
            "permission_mode": mode, "model": self.setting("model") or None,
            "policy": self.guardrail_policy(),
        }
        max_turns = self.setting("max_turns")
        if max_turns:
            payload["max_turns"] = int(max_turns)
        created = await self.bridge().create_session(payload)
        sid = str(created.get("session_id") or uuid.uuid4())
        now = _utcnow()

        def _insert():
            with get_session() as s:
                s.add(AiSession(id=sid, owner=owner, title=payload["title"], provider="claude-code",
                                model=payload["model"], permission_mode=mode, status="idle",
                                created_at=now, last_activity=now))
                s.commit()

        if self.is_ready:
            await asyncio.to_thread(retry_write, _insert)
        return self.get_session(sid) or {"id": sid, "owner": owner, "title": payload["title"], "status": "idle"}

    async def send_message(self, session_id: str, owner: str, content: str) -> dict:
        content = (content or "").strip()
        if not content:
            raise ValueError("empty message")
        now = _utcnow()

        def _record_user():
            with get_session() as s:
                row = s.get(AiSession, session_id)
                if row is not None:
                    row.status = "running"
                    row.last_activity = now
                    if row.turns == 0 and row.title == "Neue Unterhaltung":
                        row.title = content[:60] + ("…" if len(content) > 60 else "")
                s.add(AiMessage(session_id=session_id, role="user", kind="text", content=content, created_at=now))
                s.commit()

        if self.is_ready:
            await asyncio.to_thread(retry_write, _record_user)
        result = await self.bridge().send_message(session_id, content)
        self._start_recorder(session_id)
        return result or {"accepted": True}

    async def approve(self, session_id: str, approval_id: str, decision: str,
                      updated_input: dict | None = None, message: str | None = None) -> dict:
        res = await self.bridge().approve(session_id, approval_id, decision, updated_input, message)
        return res or {"ok": True}

    async def interrupt(self, session_id: str) -> dict:
        return (await self.bridge().interrupt(session_id)) or {"ok": True}

    async def close_session(self, session_id: str) -> None:
        try:
            await self.bridge().close_session(session_id)
        except BridgeError as exc:
            if exc.status != 404:
                raise

        def _archive():
            with get_session() as s:
                row = s.get(AiSession, session_id)
                if row is not None:
                    row.status = "closed"
                    row.archived = True
                    row.last_activity = _utcnow()
                    s.commit()

        if self.is_ready:
            await asyncio.to_thread(retry_write, _archive)

    # ------------------------------------------------------------------ recorder

    def _start_recorder(self, session_id: str) -> None:
        task = self._recorders.get(session_id)
        if task and not task.done():
            return
        create = getattr(self.ctx, "create_task", None) or asyncio.create_task
        self._recorders[session_id] = create(self._record_turn(session_id), name=f"ai:record:{session_id[:8]}")

    async def _record_turn(self, session_id: str) -> None:
        """Follow the bridge events of one turn and persist the transcript."""
        text_parts: list[str] = []
        try:
            async for ev in self.bridge().iter_events(session_id, after=self._last_seq(session_id)):
                kind, data = ev.get("type"), ev.get("data") or {}
                self._remember_seq(session_id, int(ev.get("seq") or 0))
                if kind == "assistant_text":
                    if data.get("delta"):
                        text_parts.append(str(data.get("text") or ""))
                    else:
                        text_parts = [str(data.get("text") or "")]
                elif kind == "tool_use":
                    await self._store(session_id, "assistant", "tool_use", None,
                                      {"name": data.get("name"), "input": data.get("input"), "tool_use_id": data.get("tool_use_id")})
                elif kind == "tool_result":
                    await self._store(session_id, "tool", "tool_result", str(data.get("output") or "")[:TRANSCRIPT_MAX_CHARS],
                                      {"name": data.get("name"), "is_error": data.get("is_error"), "tool_use_id": data.get("tool_use_id")})
                elif kind == "permission_request":
                    await self._set_status(session_id, "waiting_approval")
                elif kind == "permission_resolved":
                    await self._store(session_id, "system", "approval", None,
                                      {"approval_id": data.get("approval_id"), "decision": data.get("decision")})
                    await self._set_status(session_id, "running")
                elif kind == "error":
                    await self._store(session_id, "system", "error", str(data.get("message") or "")[:TRANSCRIPT_MAX_CHARS], None)
                    await self._finish(session_id, text_parts, None)
                    return
                elif kind == "result":
                    await self._finish(session_id, text_parts, data)
                    return
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Recorder for {session_id[:8]} stopped: {exc}")
            await self._set_status(session_id, "idle")

    def _last_seq(self, session_id: str) -> int:
        return self._seqs.get(session_id, 0)

    def _remember_seq(self, session_id: str, seq: int) -> None:
        if seq > self._seqs.get(session_id, 0):
            self._seqs[session_id] = seq

    async def _store(self, session_id: str, role: str, kind: str, content: Optional[str], meta: Optional[dict]) -> None:
        if not self.is_ready:
            return
        now = _utcnow()
        meta_json = json.loads(json.dumps(meta, default=str)) if meta else None

        def _tx():
            with get_session() as s:
                s.add(AiMessage(session_id=session_id, role=role, kind=kind, content=content, meta=meta_json, created_at=now))
                row = s.get(AiSession, session_id)
                if row is not None:
                    row.last_activity = now
                s.commit()

        await asyncio.to_thread(retry_write, _tx)

    async def _set_status(self, session_id: str, status: str) -> None:
        if not self.is_ready:
            return

        def _tx():
            with get_session() as s:
                row = s.get(AiSession, session_id)
                if row is not None:
                    row.status = status
                    row.last_activity = _utcnow()
                    s.commit()

        await asyncio.to_thread(retry_write, _tx)

    async def _finish(self, session_id: str, text_parts: list[str], result: Optional[dict]) -> None:
        text = "".join(text_parts).strip()
        if text:
            await self._store(session_id, "assistant", "text", text[:TRANSCRIPT_MAX_CHARS * 4], None)
        if not self.is_ready:
            return
        now = _utcnow()

        def _tx():
            with get_session() as s:
                row = s.get(AiSession, session_id)
                if row is not None:
                    row.status = "idle"
                    row.turns = (row.turns or 0) + 1
                    row.last_activity = now
                    if result:
                        row.total_cost_usd = float(row.total_cost_usd or 0) + float(result.get("total_cost_usd") or 0)
                        if result.get("claude_session_id"):
                            row.claude_session_id = str(result["claude_session_id"])
                if result:
                    s.add(AiMessage(session_id=session_id, role="system", kind="result", content=None,
                                    meta={k: result.get(k) for k in ("subtype", "duration_ms", "total_cost_usd", "num_turns", "is_error")},
                                    created_at=now))
                s.commit()

        await asyncio.to_thread(retry_write, _tx)


ai_service = AiAssistantService()
