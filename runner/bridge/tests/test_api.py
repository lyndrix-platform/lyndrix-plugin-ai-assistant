"""HTTP contract tests.

Plain request/response routes are exercised through httpx's in-process ASGI
transport. Server-Sent Events need a real socket: ``ASGITransport`` buffers the
whole body, so an endless ``text/event-stream`` could never be read through it.
Those tests run the app under a real uvicorn server on a random port, in the
test's own event loop, so the fake provider's asyncio primitives are shared.
"""

import asyncio
import json

import httpx
import pytest
import uvicorn

from bridge.app import create_app
from bridge.auth import AuthFlowManager

from .conftest import TOKEN, Ask, FakeProvider, Gate, assistant, result, text, wait_for


@pytest.fixture
def app(settings, provider: FakeProvider):
    return create_app(settings=settings, provider=provider, auth_flows=AuthFlowManager(None))


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge", headers={"X-Bridge-Token": TOKEN}) as c:
        yield c
    await app.state.manager.shutdown()


@pytest.fixture
async def http(app):
    """httpx client talking TCP to a real uvicorn instance of ``app`` (for SSE)."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=False, timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers={"X-Bridge-Token": TOKEN}, timeout=5) as c:
        yield c
    server.should_exit = True
    await asyncio.wait_for(task, 10)


async def test_auth_required_everywhere(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as anon:
        assert (await anon.get("/health")).status_code == 401
        assert (await anon.get("/sessions")).status_code == 401
        assert (await anon.get("/health", headers={"X-Bridge-Token": "wrong"})).status_code == 401
        assert (await anon.get("/auth/status", headers={"X-Bridge-Token": "wrong"})).status_code == 401


async def test_health(client, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LYNDRIX_API_URL", raising=False)
    body = (await client.get("/health")).json()
    assert body["status"] == "ok" and body["claude_version"] == "2.1.301" and body["auth"] == "none"
    assert body["sessions"] == 0 and body["lyndrix_api"] is False and body["auth_flow_pending"] is False
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setenv("LYNDRIX_API_URL", "http://core:8081")
    monkeypatch.setenv("LYNDRIX_API_KEY", "k")
    body = (await client.get("/health")).json()
    assert body["auth"] == "oauth" and body["lyndrix_api"] is True
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    body = (await client.get("/health")).json()
    assert body["status"] == "degraded" and body["warnings"]


async def test_session_lifecycle(http, provider, settings):
    r = await http.post("/sessions", json={"user": "marvin", "title": "T", "policy": {"allow": [{"tool": "Read"}]}})
    assert r.status_code == 201
    session_id = r.json()["session_id"]
    listed = (await http.get("/sessions")).json()
    assert listed[0]["session_id"] == session_id and listed[0]["status"] == "idle" and listed[0]["user"] == "marvin"
    assert listed[0]["policy"] == {"allow": [{"tool": "Read"}], "ask": [], "deny": []}
    detail = (await http.get(f"/sessions/{session_id}")).json()
    assert detail["cwd"] == settings.workspace and detail["events"][0]["type"] == "status"

    r = await http.post(f"/sessions/{session_id}/messages", json={"content": "hi"})
    assert r.status_code == 202 and r.json()["accepted"] is True
    await wait_for(lambda: provider.sent == ["hi"])
    await _read_events(http, f"/sessions/{session_id}/events", 5)  # turn finished: ... result, status idle
    detail = (await http.get(f"/sessions/{session_id}")).json()
    assert detail["status"] == "idle" and detail["turns"] == 1 and detail["claude_session_id"] == "claude-session-1"

    r = await http.delete(f"/sessions/{session_id}")
    assert r.json()["status"] == "closed"
    assert (await http.post(f"/sessions/{session_id}/messages", json={"content": "x"})).status_code == 409
    assert (await http.get("/sessions/nope")).status_code == 404


async def test_bad_requests(client):
    assert (await client.post("/sessions", json={"user": ""})).status_code == 422
    assert (await client.post("/sessions", json={"user": "u", "cwd": "/definitely/not/here"})).status_code == 400
    r = await client.post("/sessions", json={"user": "u", "policy": {"deny": [{"tool": "Bash", "match": "("}]}})
    assert r.status_code == 400 and r.json()["kind"] == "PolicyError"
    assert (await client.post("/sessions", json={"user": "u", "permission_mode": "yolo"})).status_code == 422


async def test_409_on_concurrent_turn_and_interrupt(client, provider):
    gate = Gate()
    provider.script = [gate, result()]
    session_id = (await client.post("/sessions", json={"user": "u"})).json()["session_id"]
    assert (await client.post(f"/sessions/{session_id}/interrupt")).status_code == 409
    assert (await client.post(f"/sessions/{session_id}/messages", json={"content": "a"})).status_code == 202
    await wait_for(lambda: provider.sent == ["a"])
    r = await client.post(f"/sessions/{session_id}/messages", json={"content": "b"})
    assert r.status_code == 409 and r.json()["kind"] == "TurnInProgress"
    assert (await client.post(f"/sessions/{session_id}/interrupt")).status_code == 202
    await wait_for(lambda: provider.interrupted == 1)
    detail = None

    async def idle():
        nonlocal detail
        detail = (await client.get(f"/sessions/{session_id}")).json()
        return detail["status"] == "idle"

    for _ in range(100):
        if await idle():
            break
        await asyncio.sleep(0.01)
    assert detail["status"] == "idle" and detail["turns"] == 1


async def _read_events(client, url, count, timeout=3.0):
    events = []

    async def read():
        async with client.stream("GET", url) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
                    if len(events) >= count:
                        return

    await asyncio.wait_for(read(), timeout)
    return events


async def test_sse_replay_after_and_live(http, provider):
    provider.script = [assistant(text("hello")), result()]
    session_id = (await http.post("/sessions", json={"user": "u"})).json()["session_id"]
    # subscribe before the turn starts so the stream has to deliver live events, not only a replay
    live = asyncio.create_task(_read_events(http, f"/sessions/{session_id}/events", 5))
    await asyncio.sleep(0.05)
    await http.post(f"/sessions/{session_id}/messages", json={"content": "hi"})
    all_events = await live
    assert [e["type"] for e in all_events] == ["status", "status", "assistant_text", "result", "status"]
    assert [e["seq"] for e in all_events] == [1, 2, 3, 4, 5]
    later = await _read_events(http, f"/sessions/{session_id}/events?after=3", 2)
    assert [e["seq"] for e in later] == [4, 5]
    assert later[0]["data"]["claude_session_id"] == "claude-session-1"


async def test_sse_keepalive_comment(http, provider):
    session_id = (await http.post("/sessions", json={"user": "u"})).json()["session_id"]
    lines = []

    async def read():
        async with http.stream("GET", f"/sessions/{session_id}/events?after=1") as r:
            async for line in r.aiter_lines():
                lines.append(line)
                if line.startswith(":"):
                    return

    await asyncio.wait_for(read(), 3)
    assert ": keep-alive" in lines


async def test_approval_endpoint(http, provider):
    provider.script = [Ask("Bash", {"command": "ls"}), result()]
    session_id = (await http.post("/sessions", json={"user": "u"})).json()["session_id"]
    await http.post(f"/sessions/{session_id}/messages", json={"content": "ls"})
    events = await _read_events(http, f"/sessions/{session_id}/events", 4)
    request = [e for e in events if e["type"] == "permission_request"][0]
    approval_id = request["data"]["approval_id"]
    assert (await http.post(f"/sessions/{session_id}/approvals/nope", json={"decision": "allow"})).status_code == 404
    assert (await http.post(f"/sessions/{session_id}/approvals/{approval_id}", json={"decision": "maybe"})).status_code == 422
    r = await http.post(f"/sessions/{session_id}/approvals/{approval_id}", json={"decision": "allow_always"})
    assert r.status_code == 200 and r.json()["decision"] == "allow_always"
    await wait_for(lambda: bool(provider.decisions) and provider.decisions[0].decision == "allow_always")
    assert (await http.post(f"/sessions/{session_id}/approvals/{approval_id}", json={"decision": "allow"})).status_code == 404
    detail = (await http.get(f"/sessions/{session_id}")).json()
    assert detail["policy"]["allow"] == [{"tool": "Bash", "match": "^ls$"}]


async def test_auth_status_without_flow(client, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    body = (await client.get("/auth/status")).json()
    assert body == {"pending": False, "authenticated": "none"}
    assert (await client.delete("/auth/whatever")).status_code == 404
    r = await client.post("/auth/start")
    assert r.status_code == 502 and "not found" in r.json()["detail"]


async def test_create_session_with_client_id_and_max_turns(client, provider):
    body = {"user": "marvin", "session_id": "plugin-session-0001", "resume": "cs-42", "max_turns": 7}
    r = await client.post("/sessions", json=body)
    assert r.status_code == 201 and r.json()["session_id"] == "plugin-session-0001"
    detail = (await client.get("/sessions/plugin-session-0001")).json()
    assert detail["claude_session_id"] == "cs-42" and detail["last_seq"] >= 1
    # the same id twice is a conflict, not a silent replacement
    r = await client.post("/sessions", json=body)
    assert r.status_code == 409 and r.json()["kind"] == "SessionExists"
    # the first turn opens the provider with the requested limits
    await client.post("/sessions/plugin-session-0001/messages", json={"content": "hi"})
    await wait_for(lambda: provider.opened)
    assert provider.opened[0].max_turns == 7 and provider.opened[0].resume == "cs-42"
    assert (await client.post("/sessions", json={"user": "x", "session_id": "short"})).status_code == 422


def test_bridge_version_comes_from_the_version_file():
    import re

    from bridge import __version__

    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__) and __version__ != "0.0.0"
