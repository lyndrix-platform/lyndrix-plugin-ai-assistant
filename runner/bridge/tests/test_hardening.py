"""AI-01/AI-03: the bridge keeps its secrets away from the agent it runs.

The agent's tools run as the bridge's own uid in the same container, so the
bridge (a) removes its secrets from the environment its children inherit,
(b) hands the CLI exactly the credential it needs — never the bridge token,
(c) is non-dumpable, and (d) refuses requests from inside the container.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from bridge import hardening
from bridge.app import create_app
from bridge.auth import AuthFlowManager
from bridge.claude_provider import SessionSpec, build_options
from bridge.config import Settings

from .conftest import TOKEN, wait_for

ENV = {
    "LYNDRIX_AI_BRIDGE_TOKEN": "dummy-bridge-token-not-real",
    "CLAUDE_CODE_OAUTH_TOKEN": "dummy-oauth-not-real",
    "LYNDRIX_API_URL": "http://core:8081",
    "LYNDRIX_API_KEY": "dummy-lyndrix-key-not-real",
    "HOME": "/home/agent",
    "PATH": "/usr/bin",
}


def test_settings_keep_the_secrets_and_the_environment_loses_them():
    env = dict(ENV)
    settings = Settings.from_env(env)
    removed = hardening.scrub_environ(env)
    assert set(removed) == {"LYNDRIX_AI_BRIDGE_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "LYNDRIX_API_URL", "LYNDRIX_API_KEY"}
    assert env == {"HOME": "/home/agent", "PATH": "/usr/bin"}           # what every child inherits
    assert settings.bridge_token == "dummy-bridge-token-not-real"
    assert settings.cli_env() == {
        "CLAUDE_CODE_OAUTH_TOKEN": "dummy-oauth-not-real",
        "LYNDRIX_API_URL": "http://core:8081",
        "LYNDRIX_API_KEY": "dummy-lyndrix-key-not-real",
    }


def test_the_cli_gets_its_credential_explicitly_and_never_the_bridge_token(tmp_path):
    settings = Settings.from_env({**ENV, "LYNDRIX_AI_WORKSPACE": str(tmp_path)})
    opts = build_options(SessionSpec(user="alice", cwd=str(tmp_path)), settings, can_use_tool=None, cli_path=None)
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == "dummy-oauth-not-real"
    assert "LYNDRIX_AI_BRIDGE_TOKEN" not in opts.env
    assert all("BRIDGE" not in key for key in opts.env)


def test_verify_uses_the_configured_credential_after_the_scrub(tmp_path):
    """`verify_token(None)` used to read the credential from os.environ, which is empty now."""
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\n"
                      "if [ \"$CLAUDE_CODE_OAUTH_TOKEN\" = dummy-oauth-not-real ] && [ -z \"$LYNDRIX_AI_BRIDGE_TOKEN\" ]; then\n"
                      "  echo '{\"is_error\": false, \"result\": \"OK\"}'; else echo '{\"is_error\": true, \"result\": \"no credential\"}'; exit 1; fi\n")
    script.chmod(0o755)
    flows = AuthFlowManager(str(script), credential_env={"CLAUDE_CODE_OAUTH_TOKEN": "dummy-oauth-not-real"})
    import asyncio

    ok, why = asyncio.run(flows.verify_token(None))
    assert ok, why
    assert flows.status()["authenticated"] == "oauth"


def test_the_bridge_process_is_not_dumpable():
    """Run in a child: PR_SET_DUMPABLE is per process and must not change the test runner."""
    code = (
        "import ctypes, sys; sys.path.insert(0, %r)\n"
        "from bridge.hardening import make_undumpable\n"
        "ok = make_undumpable()\n"
        "print(ok, ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))\n"
    ) % str(Path(__file__).resolve().parents[2])
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert out == ["True", "0"]


def test_local_addresses_and_peers(tmp_path):
    (tmp_path / "fib_trie").write_text(
        "Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 172.18.0.7\n        /32 host LOCAL\n"
        "     |-- 172.18.255.255\n        /32 link BROADCAST\n")
    (tmp_path / "if_inet6").write_text("fe800000000000000042acfffe120007 0d 40 20 80     eth0\n")
    local = hardening.local_addresses(tmp_path)
    assert "172.18.0.7" in local and "fe80::42:acff:fe12:7" in local and "172.18.255.255" not in local
    assert hardening.is_local_peer("127.0.0.1", local)
    assert hardening.is_local_peer("127.0.0.53", local)                  # all of 127/8
    assert hardening.is_local_peer("::ffff:172.18.0.7", local)
    assert hardening.is_local_peer(None, local)                           # unknown → refuse
    assert not hardening.is_local_peer("172.18.0.2", local)              # the core container
    assert not hardening.is_local_peer("testclient", local)


async def test_requests_from_inside_the_container_are_refused(tmp_path, provider):
    """The agent shares the bridge's network namespace; the plugin never does."""
    settings = Settings(bridge_token=TOKEN, workspace=str(tmp_path))          # production default: guard on
    assert settings.reject_local_peers is True
    app = create_app(settings=settings, provider=provider, auth_flows=AuthFlowManager(None))
    inside = httpx.ASGITransport(app=app)                                     # peer 127.0.0.1
    core = httpx.ASGITransport(app=app, client=("172.18.0.2", 40000))       # another container
    headers = {"X-Bridge-Token": TOKEN}
    async with httpx.AsyncClient(transport=inside, base_url="http://bridge", headers=headers) as c:
        assert (await c.get("/health")).status_code == 403
        assert (await c.post("/sessions", json={"user": "x"})).status_code == 403
        assert (await c.get("/livez")).json() == {"status": "ok"}           # Docker's HEALTHCHECK
    async with httpx.AsyncClient(transport=core, base_url="http://bridge", headers=headers) as c:
        assert (await c.get("/health")).status_code == 200
    assert Settings.from_env({"LYNDRIX_AI_BRIDGE_ALLOW_LOCAL": "1"}).reject_local_peers is False


def test_livez_needs_no_token_and_says_nothing(settings, provider):
    app = create_app(settings=settings, provider=provider, auth_flows=AuthFlowManager(None))
    with TestClient(app) as client:
        assert client.get("/livez").json() == {"status": "ok"}
        assert client.get("/health").status_code == 401


def test_workspace_settings_and_claude_md_are_not_loaded_by_default(tmp_path):
    """AI-04: /workspace is shared and agent-writable; its .claude/settings.json (hooks,
    allow rules) and CLAUDE.md must not configure other users' sessions. The plugin
    sends the instructions as append_system_prompt instead."""
    settings = Settings.from_env({"LYNDRIX_AI_WORKSPACE": str(tmp_path)})
    assert settings.setting_sources == []
    spec = SessionSpec(user="alice", cwd=str(tmp_path), append_system_prompt="# Lyndrix AI Assistant")
    opts = build_options(spec, settings, can_use_tool=None, cli_path=None)
    assert opts.setting_sources == []
    assert opts.system_prompt == {"type": "preset", "preset": "claude_code", "append": "# Lyndrix AI Assistant"}
    # explicit opt-in for a single-user setup still works
    assert Settings.from_env({"LYNDRIX_AI_SETTING_SOURCES": "project"}).setting_sources == ["project"]


async def test_guardrails_are_enforced_in_every_mode_through_a_pre_tool_use_hook(tmp_path):
    """AI-07: can_use_tool never runs in bypassPermissions, for edits in acceptEdits or
    for calls the CLI allows itself. The PreToolUse hook sees every call."""
    from bridge.claude_provider import pre_tool_use_hook
    from bridge.policy import Policy

    policy = Policy.from_dict({"deny": [{"tool": "Bash", "match": "sudo"}], "ask": [{"tool": "Edit", "match": r"\.env$"}]})

    async def guard(name, tool_input):
        verdict = policy.enforced(name, tool_input)
        return verdict, "test"

    for mode in ("default", "acceptEdits", "bypassPermissions", "plan", "auto"):
        spec = SessionSpec(user="alice", cwd=str(tmp_path), permission_mode=mode)
        opts = build_options(spec, Settings(workspace=str(tmp_path)), can_use_tool=None, cli_path=None, guard=guard)
        (matcher,) = opts.hooks["PreToolUse"]
        assert matcher.matcher is None                                   # every tool
        hook = matcher.hooks[0]
        out = await hook({"tool_name": "Bash", "tool_input": {"command": "sudo id"}}, "toolu_1", None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", mode
        out = await hook({"tool_name": "Edit", "tool_input": {"file_path": "/w/.env"}}, "toolu_2", None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "ask", mode
        assert await hook({"tool_name": "Read", "tool_input": {"file_path": "/w/a"}}, None, None) == {}
        out = await hook({"tool_name": "Bash", "tool_input": {"command": "printenv"}}, None, None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"   # built-in, every mode
    assert pre_tool_use_hook(guard) is not None
    assert build_options(SessionSpec(user="a", cwd=str(tmp_path)), Settings(), can_use_tool=None, cli_path=None).hooks is None


async def test_a_failing_guard_denies_instead_of_waving_the_call_through():
    from bridge.claude_provider import pre_tool_use_hook

    async def broken(name, tool_input):
        raise RuntimeError("boom")

    out = await pre_tool_use_hook(broken)({"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_old_plugins_without_a_system_prompt_still_get_the_workspace_instructions(tmp_path, provider):
    """An old plugin (default image :latest) picks this bridge up on its next recreate and
    sends no append_system_prompt; only the CLAUDE.md *text* is used, never through a link."""
    from bridge.app import legacy_workspace_instructions

    (tmp_path / "CLAUDE.md").write_text("# Lyndrix AI Assistant — workspace instructions\n")
    settings = Settings(bridge_token=TOKEN, workspace=str(tmp_path), reject_local_peers=False)
    app = create_app(settings=settings, provider=provider, auth_flows=AuthFlowManager(None))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge", headers={"X-Bridge-Token": TOKEN}) as c:
        old = (await c.post("/sessions", json={"user": "alice"})).json()["session_id"]
        new = (await c.post("/sessions", json={"user": "bob", "append_system_prompt": "from the plugin"})).json()["session_id"]
        for sid in (old, new):
            assert (await c.post(f"/sessions/{sid}/messages", json={"content": "hi"})).status_code == 202
        await wait_for(lambda: len(provider.opened) == 2)
    prompts = {spec.user: spec.append_system_prompt for spec in provider.opened}
    assert prompts == {"alice": "# Lyndrix AI Assistant — workspace instructions", "bob": "from the plugin"}
    (tmp_path / "CLAUDE.md").unlink()
    (tmp_path / "CLAUDE.md").symlink_to("/etc/hostname")
    assert legacy_workspace_instructions(str(tmp_path)) is None
