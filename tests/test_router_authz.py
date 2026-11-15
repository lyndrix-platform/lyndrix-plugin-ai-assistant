"""Route authorization matrix (AI-01, AI-02).

The core's plugin gate only authenticates; every route checks its own permission.
Identities are injected at ``core.api.security.authenticate_request`` so the real
``require_permission`` / ``ApiIdentity.allows`` logic of the core decides. The
service behind the router is a small fake — only who may call what is tested here.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.api.security as security
from core.api import ApiIdentity

from ai_plugin.app.api import router as router_mod

P = "lyndrix.plugin.ai_assistant"
READ, WRITE, ADMIN = f"plugin:{P}:api:read", f"plugin:{P}:api:write", f"plugin:{P}:api:admin"
CHAT, VAULT, AUTO = f"plugin:{P}:chat", f"plugin:{P}:agent_vault", f"plugin:{P}:auto_mode"

IDENTITIES = {
    # what the core's baseline gives every INT_VIEWER / INT_USER on every plugin
    "viewer": ("viewer", {"api:read"}),
    "baseline_user": ("bob", {"api:read", "api:write"}),
    # the plugin roles
    "chatter": ("alice", {READ, WRITE, CHAT}),
    "power": ("paula", {READ, WRITE, CHAT, AUTO}),
    "admin": ("root-admin", {READ, WRITE, ADMIN, CHAT, VAULT, AUTO}),
    # the assistant's own account — even when an operator grants it plugin admin
    "bot": ("ai-assistant", {READ, VAULT, ADMIN, CHAT, AUTO}),
}


class _FakeBridge:
    async def get_session(self, sid):
        return {"session_id": sid, "permission_mode": "default", "last_seq": 0}

    async def _call(self, method, path, **kw):
        return {"ok": True, "path": path}


class _FakeRunner:
    client = object()

    async def logs(self, tail=200):
        return "log"


class _FakeService:
    """Answers like AiAssistantService; records what reached it."""

    def __init__(self):
        self.rows = {
            "s-alice": {"id": "s-alice", "owner": "alice", "permission_mode": "default"},
            "s-admin": {"id": "s-admin", "owner": "root-admin", "permission_mode": "default"},
        }
        self.calls: list[tuple] = []
        self.runner = _FakeRunner()
        self.workspace = None
        self.login_last = None

    def get_session(self, sid):
        return self.rows.get(sid)

    def list_sessions(self, owner, archived=False):
        return [r for r in self.rows.values() if owner is None or r["owner"] == owner]

    def messages(self, sid):
        return []

    def bridge(self):
        return _FakeBridge()

    async def create_session(self, owner, title=None, permission_mode=None, **kw):
        self.calls.append(("create", owner, permission_mode, kw))
        return {"id": "s-new", "owner": owner, "permission_mode": permission_mode or "default"}

    async def send_message(self, sid, owner, content, **kw):
        self.calls.append(("send", sid, owner, kw))
        return {"accepted": True}

    async def approve(self, sid, aid, decision, updated_input=None, message=None):
        self.calls.append(("approve", sid, aid, decision))
        return {"resolved": True}

    async def set_permission_mode(self, sid, owner, mode, **kw):
        self.calls.append(("mode", sid, mode))
        return {"session_id": sid, "permission_mode": mode}

    async def interrupt(self, sid):
        self.calls.append(("interrupt", sid))
        return {"accepted": True}

    async def close_session(self, sid):
        self.calls.append(("close", sid))

    def reload_settings(self):
        return {}

    async def ensure_runner(self, force=False):
        self.calls.append(("restart", force))
        return {"status": "ok"}

    async def sync_workspace(self):
        self.calls.append(("sync",))
        return {"ok": True}

    def allowed_vault_spaces(self):
        return ["plugins/x"]

    def read_vault_space(self, space):
        if space != "plugins/x":
            raise KeyError(space)
        return {"k": "dummy-value-not-real"}

    def log_access(self, who, what):
        self.calls.append(("vault", who, what))

    def setting(self, key, default=None):
        return default

    async def status(self):
        return {"permission_mode": "default"}

    def instructions_text(self):
        return "# Lyndrix AI Assistant (rendered by the plugin)"

    def start_login_finisher(self, auth_id, by):
        self.calls.append(("login", auth_id, by))


@pytest.fixture
def svc(monkeypatch):
    fake = _FakeService()
    monkeypatch.setattr(router_mod, "ai_service", fake)

    def authenticate(request):
        who = request.headers.get("X-Test-Identity")
        if who not in IDENTITIES:
            return None
        username, perms = IDENTITIES[who]
        return ApiIdentity(method="user_api_key", username=username, roles=[], permissions=frozenset(perms))

    monkeypatch.setattr(security, "authenticate_request", authenticate)
    return fake


@pytest.fixture
def client(svc):
    app = FastAPI()
    app.include_router(router_mod.build_router())
    return TestClient(app)


def call(client, who, method, path, json=None):
    return client.request(method, path, json=json, headers={"X-Test-Identity": who})


@pytest.mark.parametrize("who,expected", [
    ("viewer", 403), ("baseline_user", 403), ("chatter", 201), ("admin", 201), ("bot", 403),
])
def test_starting_a_chat_needs_the_chat_permission_not_plugin_write(client, who, expected):
    """Every INT_USER holds api:write on every plugin; that must not open the runner."""
    assert call(client, who, "POST", "/sessions", {}).status_code == expected


@pytest.mark.parametrize("path,body", [
    ("/sessions/s-alice/messages", {"content": "hi"}),
    ("/sessions/s-alice/approvals/a1", {"decision": "allow"}),
    ("/sessions/s-alice/permission-mode", {"permission_mode": "default"}),
    ("/sessions/s-alice/interrupt", None),
])
def test_tool_running_routes_need_chat(client, svc, path, body):
    assert call(client, "baseline_user", "POST", path, body).status_code == 403
    assert call(client, "bot", "POST", path, body).status_code == 403
    assert call(client, "chatter", "POST", path, body).status_code in (200, 202)


def test_the_assistant_account_is_never_plugin_admin(client, svc):
    """Its key is reachable from every chat (AI-01): no foreign sessions, no runner or login control."""
    assert call(client, "bot", "GET", "/sessions?all=true").json() == {"sessions": []}
    assert call(client, "bot", "GET", "/sessions/s-alice").status_code == 404
    assert call(client, "bot", "GET", "/sessions/s-alice/events").status_code == 404
    for method, path in (("POST", "/runner/restart"), ("GET", "/runner/logs"), ("POST", "/workspace/sync"),
                         ("POST", "/auth/start"), ("POST", "/auth/verify"), ("GET", "/auth/status"),
                         ("DELETE", "/auth/x1")):
        assert call(client, "bot", method, path).status_code == 403, path
    assert not [c for c in svc.calls if c[0] in ("restart", "sync")]
    # a human admin still has all of it
    assert len(call(client, "admin", "GET", "/sessions?all=true").json()["sessions"]) == 2
    assert call(client, "admin", "GET", "/sessions/s-alice").status_code == 200
    assert call(client, "admin", "POST", "/runner/restart").status_code == 200


def test_users_only_see_their_own_sessions(client):
    assert [s["id"] for s in call(client, "chatter", "GET", "/sessions?all=true").json()["sessions"]] == ["s-alice"]
    assert call(client, "chatter", "GET", "/sessions/s-admin").status_code == 404
    assert call(client, "chatter", "POST", "/sessions/s-admin/approvals/a1", {"decision": "allow"}).status_code == 404


def test_vault_values_need_agent_vault_not_plugin_admin(client, svc):
    assert call(client, "viewer", "GET", "/vault/spaces").status_code == 200        # names only
    assert call(client, "chatter", "GET", "/vault/spaces/plugins/x").status_code in (403, 404)
    assert call(client, "chatter", "GET", "/vault/spaces/x").status_code == 403
    assert call(client, "baseline_user", "GET", "/vault/spaces/x").status_code == 403
    assert call(client, "bot", "GET", "/vault/spaces/nope").status_code == 404
    svc.allowed_vault_spaces = lambda: ["x"]
    svc.read_vault_space = lambda space: {"k": "dummy-value-not-real"} if space == "x" else (_ for _ in ()).throw(KeyError(space))
    res = call(client, "bot", "GET", "/vault/spaces/x")
    assert res.status_code == 200 and res.json()["values"] == {"k": "dummy-value-not-real"}
    assert ("vault", "ai-assistant", "vault space 'x'") in svc.calls


def test_unauthenticated_requests_are_refused(client):
    assert call(client, "nobody", "GET", "/status").status_code == 401
    assert call(client, "nobody", "POST", "/sessions", {}).status_code == 401


def test_manifest_roles_match_the_route_permissions():
    """The roles the operator assigns must carry exactly what the routes check."""
    from ai_plugin.entrypoint import manifest

    roles = {r.id if hasattr(r, "id") else r["id"]: set(r.permissions if hasattr(r, "permissions") else r["permissions"])
             for r in manifest.roles}
    custom = {c.id if hasattr(c, "id") else c["id"] for c in manifest.custom_permissions}
    assert {"api:admin", "chat", "agent_vault", "auto_mode"} <= custom
    assert CHAT in roles["user"] and ADMIN not in roles["user"]
    assert roles["agent"] == {READ, VAULT}                 # never api:admin, never chat
    assert {ADMIN, CHAT, VAULT, AUTO} <= roles["admin"]
    assert AUTO in roles["power_user"] and AUTO not in roles["user"] and ADMIN not in roles["power_user"]
    assert CHAT not in roles["viewer"]


@pytest.mark.parametrize("who,mode,expected", [
    ("chatter", "default", 201), ("chatter", "acceptEdits", 201), ("chatter", "plan", 201),
    ("chatter", "auto", 403), ("chatter", "bypassPermissions", 403),
    ("power", "auto", 201), ("power", "bypassPermissions", 403),
    ("admin", "auto", 201), ("admin", "bypassPermissions", 201),
])
def test_session_start_mode_is_checked_at_create(client, svc, who, mode, expected):
    """AI-02: bypassPermissions used to be admin-only on switch but not on create."""
    res = call(client, who, "POST", "/sessions", {"permission_mode": mode})
    assert res.status_code == expected, res.text
    if expected == 201:
        allowed = svc.calls[-1][3]["allowed_modes"]
        assert mode in allowed and ("bypassPermissions" in allowed) == (who == "admin")


@pytest.mark.parametrize("who,mode,expected", [
    ("chatter", "auto", 403), ("chatter", "bypassPermissions", 403), ("chatter", "acceptEdits", 200),
    ("power", "auto", 200), ("power", "bypassPermissions", 403), ("admin", "bypassPermissions", 200),
])
def test_mode_switch_is_checked(client, svc, who, mode, expected):
    sid = "s-alice" if who in ("chatter", "admin") else "s-paula"
    svc.rows["s-paula"] = {"id": "s-paula", "owner": "paula", "permission_mode": "default"}
    assert call(client, who, "POST", f"/sessions/{sid}/permission-mode", {"permission_mode": mode}).status_code == expected


def test_messages_carry_the_callers_modes_to_the_service(client, svc):
    call(client, "chatter", "POST", "/sessions/s-alice/messages", {"content": "hi"})
    assert svc.calls[-1][0] == "send" and svc.calls[-1][3]["allowed_modes"] == ("default", "acceptEdits", "plan")


def test_status_tells_the_ui_what_the_caller_may_do(client):
    assert call(client, "baseline_user", "GET", "/status").json()["caller"] == {
        "can_chat": False, "is_admin": False, "permission_modes": ["default", "acceptEdits", "plan"]}
    assert call(client, "admin", "GET", "/status").json()["caller"]["permission_modes"] == [
        "default", "acceptEdits", "plan", "auto", "bypassPermissions"]
    bot = call(client, "bot", "GET", "/status").json()["caller"]
    assert bot["can_chat"] is False and bot["is_admin"] is False


def test_instructions_come_from_the_plugin_not_the_workspace_file(client, svc, tmp_path):
    """AI-04: any chat session can rewrite /workspace/CLAUDE.md; the API shows what the agent gets."""
    body = call(client, "viewer", "GET", "/instructions").json()
    assert body == {"markdown": "# Lyndrix AI Assistant (rendered by the plugin)", "source": "plugin"}
