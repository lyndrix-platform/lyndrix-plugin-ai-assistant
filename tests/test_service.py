"""Service behaviour that needs no runner: settings → guardrail policy and
credentials, session records + transcript recording against a fake bridge."""
import asyncio
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_plugin.app.logic import service as service_mod
from ai_plugin.app.logic.access import parse_vault_spaces
from ai_plugin.app.logic.service import AiAssistantService
from ai_plugin.app.model import database as db_mod
from ai_plugin.app.model.models import AiMessage, AiSession, _Base


class _Ctx:
    def __init__(self, settings=None):
        self._settings = settings or {}
        self.secrets = {}
        self.events = []

    def get_all_settings(self):
        return dict(self._settings)

    def get_secret(self, key):
        return self.secrets.get(key)

    def set_secret(self, key, value):
        self.secrets[key] = value
        return True

    def create_task(self, coro, name=None):
        return asyncio.get_running_loop().create_task(coro)


class _FakeBridge:
    """Answers like the runner bridge; events are scripted per session."""

    def __init__(self):
        self.created = []
        self.sent = []
        self.events = {}
        self.lost = set()        # session ids the bridge forgot (runner restart)
        self.last_seq = {}

    async def create_session(self, payload):
        self.created.append(payload)
        return {"session_id": f"s{len(self.created)}", "created_at": "2026-10-16T00:00:00"}

    async def send_message(self, sid, content):
        self.sent.append((sid, content))
        return {"accepted": True}

    async def set_permission_mode(self, sid, mode):
        self.modes = getattr(self, "modes", []) + [(sid, mode)]
        return {"session_id": sid, "permission_mode": mode}

    async def get_session(self, sid):
        if sid in self.lost:
            from ai_plugin.app.logic.bridge import BridgeError

            raise BridgeError(404, "unknown session")
        return {"session_id": sid, "last_seq": self.last_seq.get(sid, 0)}

    async def iter_events(self, sid, after=0):
        for ev in self.events.get(sid, []):
            if ev["seq"] > after:
                yield ev


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    _Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    dbi = db_mod.db_instance
    monkeypatch.setattr(dbi, "engine", engine, raising=False)
    monkeypatch.setattr(dbi, "SessionLocal", Session, raising=False)
    monkeypatch.setattr(dbi, "is_connected", True, raising=False)

    @contextmanager
    def _lock(name, timeout=None):
        yield True

    monkeypatch.setattr(dbi, "advisory_lock", _lock, raising=False)
    return Session


def _svc(ctx, bridge):
    svc = AiAssistantService()
    svc.ctx = ctx
    svc.reload_settings()
    svc._tables_ready = True
    svc.bridge = lambda: bridge  # type: ignore[method-assign]
    return svc


def test_guardrail_policy_and_credentials():
    svc = AiAssistantService()
    svc.ctx = _Ctx({
        "guardrails_allow": "Read; Grep; Bash: ^git (status|log)",
        "guardrails_deny": "Bash: rm -rf\n# comment",
        "claude_oauth_token": "tok", "anthropic_api_key": "key", "claude_auth_mode": "oauth_token",
        "vault_spaces": "plugins/a, plugins/b,,../etc",
    })
    svc.reload_settings()
    policy = svc.guardrail_policy()
    assert policy["allow"] == [{"tool": "Read"}, {"tool": "Grep"}, {"tool": "Bash", "match": "^git (status|log)"}]
    assert policy["deny"] == [{"tool": "Bash", "match": "rm -rf"}] and policy["ask"] == []
    assert svc.auth_env() == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"} and svc.auth_mode() == "oauth"
    svc.settings["claude_auth_mode"] = "api_key"
    assert svc.auth_env() == {"ANTHROPIC_API_KEY": "key"} and svc.auth_mode() == "api_key"
    svc.settings.pop("anthropic_api_key")
    svc.settings.pop("claude_oauth_token")
    assert svc.auth_env() == {} and svc.auth_mode() == "none"
    assert parse_vault_spaces("plugins/a, plugins/b,,../etc") == ["plugins/a", "plugins/b"]
    assert svc.allowed_vault_spaces() == ["plugins/a", "plugins/b"]


def test_session_lifecycle_and_transcript(db):
    bridge = _FakeBridge()
    svc = _svc(_Ctx({"permission_mode": "acceptEdits", "max_turns": "5"}), bridge)

    async def run():
        s = await svc.create_session("marvin", None)
        assert s["owner"] == "marvin" and s["status"] == "idle" and s["permission_mode"] == "acceptEdits"
        assert bridge.created[0]["policy"] == {"allow": [], "ask": [], "deny": []}
        assert bridge.created[0]["max_turns"] == 5
        sid = s["id"]
        bridge.events[sid] = [
            {"seq": 1, "type": "status", "data": {"state": "running"}},
            {"seq": 2, "type": "assistant_text", "data": {"text": "Let me ", "delta": True}},
            {"seq": 3, "type": "assistant_text", "data": {"text": "check.", "delta": True}},
            {"seq": 4, "type": "tool_use", "data": {"tool_use_id": "t1", "name": "Read", "input": {"file_path": "/workspace/README.md"}}},
            {"seq": 5, "type": "tool_result", "data": {"tool_use_id": "t1", "name": "Read", "is_error": False, "output": "# Lyndrix AI workspace"}},
            {"seq": 6, "type": "permission_request", "data": {"approval_id": "a1", "name": "Bash", "input": {"command": "ls"}}},
            {"seq": 7, "type": "permission_resolved", "data": {"approval_id": "a1", "decision": "allow"}},
            {"seq": 8, "type": "result", "data": {"subtype": "success", "total_cost_usd": 0.0123, "num_turns": 3, "claude_session_id": "cs-1"}},
        ]
        await svc.send_message(sid, "marvin", "What is in the workspace README?")
        await asyncio.wait_for(svc._recorders[sid], 5)
        return sid

    sid = asyncio.run(run())
    with db() as s:
        row = s.get(AiSession, sid)
        assert row.status == "idle" and row.turns == 1 and row.claude_session_id == "cs-1"
        assert abs(row.total_cost_usd - 0.0123) < 1e-9
        assert row.title.startswith("What is in the workspace")
        kinds = [(m.role, m.kind) for m in s.query(AiMessage).filter(AiMessage.session_id == sid).order_by(AiMessage.id)]
    # text that arrived before a tool call is stored before the tool card (chat order)
    assert kinds == [("user", "text"), ("assistant", "text"), ("assistant", "tool_use"), ("tool", "tool_result"),
                     ("system", "approval"), ("system", "result")]
    msgs = svc.messages(sid)
    assert [m["content"] for m in msgs if m["kind"] == "text"] == ["What is in the workspace README?", "Let me check."]
    assert svc.list_sessions("marvin")[0]["id"] == sid and svc.list_sessions("someone-else") == []
    assert bridge.sent == [(sid, "What is in the workspace README?")]


def test_error_event_ends_the_turn(db):
    bridge = _FakeBridge()
    svc = _svc(_Ctx({}), bridge)

    async def run():
        s = await svc.create_session("marvin", "Test")
        bridge.events[s["id"]] = [{"seq": 1, "type": "error", "data": {"message": "invalid bearer token"}}]
        await svc.send_message(s["id"], "marvin", "hello")
        await asyncio.wait_for(svc._recorders[s["id"]], 5)
        return s["id"]

    sid = asyncio.run(run())
    with db() as s:
        row = s.get(AiSession, sid)
        assert row.status == "idle" and row.turns == 1
        errors = [m for m in s.query(AiMessage).filter(AiMessage.session_id == sid) if m.kind == "error"]
        assert errors and "invalid bearer token" in errors[0].content
    assert service_mod.PERMISSION_MODES[0] == "default"


def test_bridge_session_recreated_after_runner_restart(db):
    bridge = _FakeBridge()
    svc = _svc(_Ctx({}), bridge)

    async def run():
        s = await svc.create_session("marvin", "Test")
        sid = s["id"]
        bridge.events[sid] = [{"seq": 1, "type": "result", "data": {"subtype": "success", "claude_session_id": "cs-9", "result": "done"}}]
        await svc.send_message(sid, "marvin", "first")
        await asyncio.wait_for(svc._recorders[sid], 5)
        # runner restarted: the bridge forgot the session, the DB still has it
        bridge.lost.add(sid)
        bridge.events[sid] = [{"seq": 1, "type": "result", "data": {"subtype": "success"}}]
        await svc.send_message(sid, "marvin", "second")
        await asyncio.wait_for(svc._recorders[sid], 5)
        return sid

    sid = asyncio.run(run())
    assert len(bridge.created) == 2
    recreated = bridge.created[1]
    assert recreated["session_id"] == sid and recreated["resume"] == "cs-9" and recreated["user"] == "marvin"
    with db() as s:
        row = s.get(AiSession, sid)
        assert row.turns == 2
        results = [m for m in s.query(AiMessage).filter(AiMessage.session_id == sid) if m.kind == "result"]
        assert results[0].meta["result"] == "done"


def test_permission_mode_switch_updates_bridge_and_row(db):
    bridge = _FakeBridge()
    svc = _svc(_Ctx({}), bridge)

    async def run():
        s = await svc.create_session("marvin", "Modes")
        await svc.set_permission_mode(s["id"], "marvin", "auto")
        try:
            await svc.set_permission_mode(s["id"], "marvin", "yolo")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid mode accepted")
        return s["id"]

    sid = asyncio.run(run())
    assert bridge.modes == [(sid, "auto")]
    with db() as s:
        assert s.get(AiSession, sid).permission_mode == "auto"
