"""AI-12: transcripts keep no credential-shaped strings, can be purged and expire."""
import asyncio
from datetime import timedelta

import pytest

from ai_plugin.app.logic import service as service_mod
from ai_plugin.app.logic.redact import REDACTED, redact, redact_obj
from ai_plugin.app.model.models import AiMessage, AiSession

from .test_service import _Ctx, _FakeBridge, _svc, db  # noqa: F401  (fixture re-export)

SECRETS = {
    "anthropic": "sk-ant-oat01-" + "A" * 40,
    "lyndrix": "lyk_" + "b" * 43,
    "gitlab": "glpat-" + "c" * 20,
    "github": "ghp_" + "d" * 36,
    "vault": "hvs." + "e" * 24,
}


@pytest.mark.parametrize("name,value", sorted(SECRETS.items()))
def test_known_token_formats_are_redacted(name, value):
    assert redact(f"export TOKEN={value} # {name}") == f"export TOKEN={REDACTED} # {name}"


def test_authorization_values_and_private_keys_are_redacted():
    assert redact("Authorization: Bearer abcdefghijklmnop1234") == "Authorization: Bearer " + REDACTED
    assert redact("authorization: Basic dXNlcjpwYXNzd29yZA==") == "authorization: Basic " + REDACTED
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----"
    assert redact(f"key:\n{pem}\ndone") == f"key:\n{REDACTED}\ndone"


def test_ordinary_text_is_left_alone():
    for text in ("a basic understanding of internationalization", "Bearer short", "sk-ant-short",
                 "the lyk_ prefix", "git status", None, ""):
        assert redact(text) == text
    assert redact_obj({"input": {"command": f"echo {SECRETS['anthropic']}"}, "n": 1}) == {
        "input": {"command": f"echo {REDACTED}"}, "n": 1}


def test_nothing_token_shaped_reaches_the_transcript(db):
    bridge = _FakeBridge()
    svc = _svc(_Ctx({}), bridge)

    async def run():
        s = await svc.create_session("alice", None)
        sid = s["id"]
        bridge.events[sid] = [
            {"seq": 1, "type": "tool_use", "data": {"tool_use_id": "t1", "name": "Bash",
                                                    "input": {"command": f"curl -H 'x-api-key: {SECRETS['anthropic']}'"}}},
            {"seq": 2, "type": "tool_result", "data": {"tool_use_id": "t1", "name": "Bash", "output": f"LYNDRIX_API_KEY={SECRETS['lyndrix']}"}},
            {"seq": 3, "type": "result", "data": {"subtype": "success", "result": f"token is {SECRETS['github']}"}},
        ]
        await svc.send_message(sid, "alice", f"use {SECRETS['gitlab']} please")
        await asyncio.wait_for(svc._recorders[sid], 5)
        return sid

    sid = asyncio.run(run())
    with db() as s:
        stored = " ".join(f"{m.content} {m.meta}" for m in s.query(AiMessage).filter(AiMessage.session_id == sid))
        title = s.get(AiSession, sid).title
    assert REDACTED in stored
    for value in SECRETS.values():
        if value != SECRETS["vault"]:
            assert value not in stored and value not in title


def test_purge_deletes_the_conversation_for_good(db, monkeypatch):
    monkeypatch.setattr(service_mod, "PURGE_BATCH", 2)          # several batches
    bridge = _FakeBridge()
    closed = []

    async def close_session(sid):
        closed.append(sid)

    bridge.close_session = close_session
    svc = _svc(_Ctx({}), bridge)

    async def run():
        keep = await svc.create_session("bob", "keep")
        gone = await svc.create_session("alice", "gone")
        for sid in (keep["id"], gone["id"]):
            for i in range(5):
                await svc._store(sid, "assistant", "text", f"line {i}", None)
        return keep["id"], gone["id"], await svc.purge_session(gone["id"])

    keep, gone, res = asyncio.run(run())
    assert res == {"ok": True, "purged": gone, "messages": 5} and closed == [gone]
    with db() as s:
        assert s.get(AiSession, gone) is None and s.get(AiSession, keep) is not None
        assert s.query(AiMessage).filter(AiMessage.session_id == gone).count() == 0
        assert s.query(AiMessage).filter(AiMessage.session_id == keep).count() == 5


def test_retention_deletes_only_old_idle_conversations(db):
    bridge = _FakeBridge()
    svc = _svc(_Ctx({"transcript_retention_days": "30"}), bridge)

    async def run():
        ids = {}
        for name in ("old", "old_running", "recent"):
            ids[name] = (await svc.create_session(name, name))["id"]
            await svc._store(ids[name], "user", "text", "hi", None)
        return ids

    ids = asyncio.run(run())
    with db() as s:
        for name in ("old", "old_running"):
            row = s.get(AiSession, ids[name])
            row.last_activity = service_mod._utcnow() - timedelta(days=31)
        s.get(AiSession, ids["old_running"]).status = "running"
        s.commit()
    assert svc.prune_transcripts() == {"ok": True, "sessions": 1, "messages": 1, "retention_days": 30}
    with db() as s:
        assert s.get(AiSession, ids["old"]) is None
        assert s.get(AiSession, ids["old_running"]) is not None and s.get(AiSession, ids["recent"]) is not None
    svc.settings["transcript_retention_days"] = "0"
    assert svc.prune_transcripts()["sessions"] == 0                 # off by default


def test_purge_during_a_running_turn_leaves_nothing_behind(db):
    """Review of AI-12: the running turn's recorder kept writing after the delete."""
    from .test_service import _SlowBridge

    bridge = _SlowBridge()

    async def close_session(sid):
        pass

    bridge.close_session = close_session
    svc = _svc(_Ctx({}), bridge)

    async def run():
        s = await svc.create_session("alice", "x")
        sid = s["id"]
        bridge.events[sid] = [
            {"seq": 1, "type": "status", "data": {"state": "running"}},
            {"seq": 2, "type": "tool_use", "data": {"tool_use_id": "t1", "name": "Bash", "input": {"command": "sleep 60"}}},
            {"seq": 3, "type": "assistant_text", "data": {"text": "still talking", "delta": True}},
        ]
        await svc.send_message(sid, "alice", "go")
        for _ in range(100):
            if svc._last_seq(sid) == 3:
                break
            await asyncio.sleep(0.01)
        await svc.purge_session(sid)
        await svc._store(sid, "assistant", "text", "a late writer", None)    # refused
        await svc._finish(sid, ["late"], {"subtype": "success"})              # refused
        return sid

    sid = asyncio.run(run())
    with db() as s:
        assert s.get(AiSession, sid) is None
        assert s.query(AiMessage).filter(AiMessage.session_id == sid).count() == 0


def test_retention_also_sweeps_orphaned_entries(db):
    from datetime import datetime

    svc = _svc(_Ctx({"transcript_retention_days": "30"}), _FakeBridge())
    with db() as s:
        s.add(AiMessage(session_id="gone-session", role="tool", kind="tool_result", content="x",
                        created_at=datetime(2026, 1, 1)))
        s.commit()
    assert svc.prune_transcripts()["messages"] == 1
    with db() as s:
        assert s.query(AiMessage).count() == 0
