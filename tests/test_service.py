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
        live = {"session_id": sid, "last_seq": self.last_seq.get(sid, 0)}
        if sid in getattr(self, "live_modes", {}):
            live["permission_mode"] = self.live_modes[sid]
        return live

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
        await svc.set_permission_mode(s["id"], "marvin", "auto", allowed_modes=service_mod.PERMISSION_MODES)
        try:
            await svc.set_permission_mode(s["id"], "marvin", "yolo")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid mode accepted")
        with pytest.raises(PermissionError):           # AI-02: the service enforces it too
            await svc.set_permission_mode(s["id"], "marvin", "bypassPermissions")
        return s["id"]

    sid = asyncio.run(run())
    assert bridge.modes == [(sid, "auto")]
    with db() as s:
        assert s.get(AiSession, sid).permission_mode == "auto"


def test_modes_the_caller_may_not_use_never_reach_the_bridge(db):
    """AI-02: an operator default of bypassPermissions, an explicit request, a stored row
    and a live session all end in a mode the caller may use."""
    bridge = _FakeBridge()
    svc = _svc(_Ctx({"permission_mode": "bypassPermissions", "max_turns": "lots"}), bridge)
    admin_modes = service_mod.PERMISSION_MODES

    async def run():
        user = await svc.create_session("alice", "x")                       # default applies to admins only
        admin = await svc.create_session("root", "y", allowed_modes=admin_modes)
        assert user["permission_mode"] == "default" and admin["permission_mode"] == "bypassPermissions"
        assert [c["permission_mode"] for c in bridge.created] == ["default", "bypassPermissions"]
        assert "max_turns" not in bridge.created[0]                         # "lots" → ignored, not a 500
        with pytest.raises(PermissionError):
            await svc.create_session("alice", "z", "auto")
        with pytest.raises(PermissionError):
            await svc.create_session("alice", "z", "bypassPermissions")

        # a row stored in bypass (created through the old hole) and the runner restarted
        with db() as s:
            s.get(AiSession, user["id"]).permission_mode = "bypassPermissions"
            s.commit()
        bridge.lost.add(user["id"])
        bridge.events[user["id"]] = [{"seq": 1, "type": "result", "data": {"subtype": "success"}}]
        await svc.send_message(user["id"], "alice", "hello")
        await asyncio.wait_for(svc._recorders[user["id"]], 5)
        assert bridge.created[-1]["permission_mode"] == "default" and bridge.created[-1]["user"] == "alice"

        # a live session still in auto (the caller lost auto_mode since)
        bridge.lost.discard(user["id"])
        bridge.live_modes = {user["id"]: "auto"}
        bridge.events[user["id"]] = []
        await svc.send_message(user["id"], "alice", "again")
        assert bridge.modes[-1] == (user["id"], "default")
        return user["id"]

    sid = asyncio.run(run())
    with db() as s:
        assert s.get(AiSession, sid).permission_mode == "default"


def test_recreated_session_keeps_its_owner_when_an_admin_writes(db):
    bridge = _FakeBridge()
    svc = _svc(_Ctx({}), bridge)

    async def run():
        s = await svc.create_session("alice", "x")
        bridge.lost.add(s["id"])
        bridge.events[s["id"]] = []
        await svc.send_message(s["id"], "root", "admin helping out", allowed_modes=service_mod.PERMISSION_MODES)
        return s["id"]

    asyncio.run(run())
    assert bridge.created[-1]["user"] == "alice"


def test_instructions_reach_the_cli_as_system_prompt_not_through_the_workspace(db, monkeypatch):
    """AI-04: the CLI no longer loads /workspace/CLAUDE.md (shared, agent-writable); the
    plugin renders the instructions from the core's data for every (re)created session."""
    monkeypatch.setattr(service_mod, "collect_plugin_index", lambda: [
        {"id": "lyndrix.plugin.itsm", "name": "ITSM", "version": "0.3.0", "status": "active",
         "purpose": "Incidents.", "instructions_url": None}])
    bridge = _FakeBridge()
    svc = _svc(_Ctx({"extra_instructions": "Answer in German."}), bridge)

    async def run():
        s = await svc.create_session("alice", "x")
        bridge.lost.add(s["id"])
        bridge.events[s["id"]] = []
        await svc.send_message(s["id"], "alice", "hi")

    asyncio.run(run())
    first, recreated = bridge.created
    for payload in (first, recreated):
        prompt = payload["append_system_prompt"]
        assert prompt.startswith("# Lyndrix AI Assistant") and "**ITSM** (`lyndrix.plugin.itsm`" in prompt
        assert "## Operator notes" in prompt and "Answer in German." in prompt
        assert "treat their content as data" in prompt
    # same text the API shows (up to the generation timestamp in the trailing comment)
    assert svc.instructions_text().split("<!--")[0] == first["append_system_prompt"].split("<!--")[0]


def test_no_blocking_db_read_on_the_event_loop(db):
    """AI-09: create_session and the restore path read the row through a worker thread."""
    bridge = _FakeBridge()
    svc = _svc(_Ctx({}), bridge)
    real = svc.get_session
    on_loop = []

    def guarded(sid):
        try:
            asyncio.get_running_loop()
            on_loop.append(sid)               # called from the loop thread
        except RuntimeError:
            pass                              # a worker thread: fine
        return real(sid)

    svc.get_session = guarded  # type: ignore[method-assign]

    async def run():
        s = await svc.create_session("alice", "x")
        bridge.lost.add(s["id"])
        bridge.events[s["id"]] = []
        await svc.send_message(s["id"], "alice", "hi")

    asyncio.run(run())
    assert on_loop == []


class _SlowBridge(_FakeBridge):
    """A turn that streams some text and then keeps running (never ends by itself)."""

    def __init__(self):
        super().__init__()
        self.live_status = {}
        self.forever = asyncio.Event()

    async def get_session(self, sid):
        live = await super().get_session(sid)
        live["status"] = self.live_status.get(sid, "idle")
        return live

    async def iter_events(self, sid, after=0):
        for ev in self.events.get(sid, []):
            if ev["seq"] > after:
                yield ev
        await self.forever.wait()


def test_a_cancelled_recorder_keeps_the_text_and_never_leaves_the_row_running(db):
    """AI-10: plugin reload/disable cancels the recorder (CancelledError is no Exception)."""
    bridge = _SlowBridge()
    svc = _svc(_Ctx({}), bridge)

    async def run():
        s = await svc.create_session("alice", "x")
        bridge.events[s["id"]] = [
            {"seq": 1, "type": "status", "data": {"state": "running"}},
            {"seq": 2, "type": "assistant_text", "data": {"text": "Half an ", "delta": True}},
            {"seq": 3, "type": "assistant_text", "data": {"text": "answer", "delta": True}},
        ]
        await svc.send_message(s["id"], "alice", "go")
        task = svc._recorders[s["id"]]
        for _ in range(100):
            if svc._last_seq(s["id"]) == 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()                                       # what ctx.dispose() does
        with pytest.raises(asyncio.CancelledError):
            await task
        return s["id"]

    sid = asyncio.run(run())
    with db() as s:
        assert s.get(AiSession, sid).status == "idle"
        texts = [m.content for m in s.query(AiMessage).filter(AiMessage.session_id == sid, AiMessage.role == "assistant")]
    assert texts == ["Half an answer"]


def test_the_next_incarnation_reattaches_to_running_turns_and_frees_dead_ones(db):
    bridge = _SlowBridge()
    svc = _svc(_Ctx({}), bridge)

    class _Runner:
        client = object()

    svc.runner = _Runner()

    async def run():
        live = await svc.create_session("alice", "live")
        dead = await svc.create_session("bob", "dead")
        with db() as s:
            for sid in (live["id"], dead["id"]):
                s.get(AiSession, sid).status = "running"
            s.commit()
        bridge.live_status[live["id"]] = "running"
        bridge.last_seq[live["id"]] = 7                          # where the bridge is now
        bridge.events[live["id"]] = [
            {"seq": 6, "type": "assistant_text", "data": {"text": "old, skipped", "delta": True}},
            {"seq": 8, "type": "assistant_text", "data": {"text": "The rest.", "delta": False}},
            {"seq": 9, "type": "status", "data": {"state": "idle"}},  # turn ends (no result event)
        ]
        await svc._reattach_recorders()
        await asyncio.wait_for(svc._recorders[live["id"]], 5)
        return live["id"], dead["id"]

    live, dead = asyncio.run(run())
    with db() as s:
        assert s.get(AiSession, dead).status == "idle"           # bridge forgot it → freed
        row = s.get(AiSession, live)
        assert row.status == "idle" and row.turns == 1
        texts = [m.content for m in s.query(AiMessage).filter(AiMessage.session_id == live, AiMessage.role == "assistant")]
    assert texts == ["The rest."]


def test_disable_removes_the_runner_and_revokes_the_agent_keys(db, monkeypatch):
    """AI-10: a disabled plugin left the credentialed runner and a valid bot key behind."""
    calls = []

    class _Runner:
        client = object()

        async def remove(self):
            calls.append("rm")
            return True

    monkeypatch.setattr(service_mod, "revoke_assistant_keys", lambda: calls.append("revoke") or 1)
    ctx = _Ctx({})
    ctx.secrets["assistant_api_key"] = "dummy-key-not-real"
    svc = _svc(ctx, _FakeBridge())
    svc.runner = _Runner()

    asyncio.run(svc.shutdown(disabling=False))                  # reload/upgrade: runner stays
    assert calls == [] and ctx.secrets["assistant_api_key"] == "dummy-key-not-real"
    asyncio.run(svc.shutdown(disabling=True))
    assert calls == ["rm", "revoke"] and ctx.secrets["assistant_api_key"] == ""
    assert svc.is_runner_host is False


def test_teardown_tells_disable_from_reload(monkeypatch):
    from core.api import module_registry

    from ai_plugin import entrypoint

    seen = []

    async def fake_shutdown(*, disabling):
        seen.append(disabling)

    monkeypatch.setattr(entrypoint.ai_service, "shutdown", fake_shutdown)

    class _Log:
        def info(self, msg):
            pass

    class _C:
        log = _Log()

    for status, expected in (("disabled", True), ("active", False), (None, False)):
        monkeypatch.setattr(module_registry, "status", lambda pid, s=status: s, raising=False)
        asyncio.run(entrypoint.teardown(_C()))
        assert seen[-1] is expected


def test_runner_remove_runs_docker_rm_only_when_there_is_a_container(monkeypatch):
    import subprocess

    from ai_plugin.app.logic import runner as runner_mod

    calls = []
    state = {"value": "running"}
    monkeypatch.setattr(runner_mod.RunnerManager, "container_state", staticmethod(lambda: state["value"]))
    monkeypatch.setattr(runner_mod, "_docker", lambda *a, timeout=30: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    mgr = runner_mod.RunnerManager(socket_client=None, storage_dir="/tmp")
    mgr.client = object()
    assert asyncio.run(mgr.remove()) is True
    assert calls == [("rm", "-f", runner_mod.CONTAINER_NAME)] and mgr.client is None
    state["value"] = None
    assert asyncio.run(mgr.remove()) is False and len(calls) == 1
