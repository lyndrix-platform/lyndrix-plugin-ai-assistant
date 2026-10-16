import asyncio
import re

import pytest

from bridge.claude_provider import SessionSpec
from bridge.policy import Policy
from bridge.sessions import STATUS_CLOSED, STATUS_IDLE, STATUS_RUNNING, STATUS_WAITING, SessionManager, TurnInProgress

from .conftest import Ask, FakeProvider, Gate, Raise, assistant, result, text, tool_result, tool_use, wait_for


def types(session):
    return [e.type for e in session.events.replay(0)]


async def test_turn_emits_events_and_updates_session(manager: SessionManager, provider: FakeProvider, spec):
    provider.script = [
        assistant(text("Looking"), tool_use("t1", "Read", file_path="/w/a.py")),
        tool_result("t1", "content"),
        assistant(text("Done")),
        result(session_id="cs-1", cost=0.02),
    ]
    session = manager.create(spec)
    task = manager.start_turn(session, "hi")
    await task
    assert types(session) == ["status", "status", "assistant_text", "tool_use", "tool_result", "assistant_text", "result", "status"]
    assert session.status == STATUS_IDLE
    assert session.turns == 1
    assert session.claude_session_id == "cs-1"
    assert session.total_cost_usd == pytest.approx(0.02)
    assert provider.sent == ["hi"]


async def test_provider_session_is_reused_between_turns(manager, provider, spec):
    session = manager.create(spec)
    await manager.start_turn(session, "one")
    await manager.start_turn(session, "two")
    assert len(provider.opened) == 1
    assert session.turns == 2


async def test_cost_accumulates_as_delta_of_cumulative_reports(manager, provider, spec):
    costs = iter([0.10, 0.25])
    provider.script = lambda content: [result(cost=next(costs))]
    session = manager.create(spec)
    await manager.start_turn(session, "a")
    await manager.start_turn(session, "b")
    assert session.total_cost_usd == pytest.approx(0.25)


async def test_concurrent_turn_is_rejected(manager, provider, spec):
    gate = Gate()
    provider.script = [gate, result()]
    session = manager.create(spec)
    task = manager.start_turn(session, "first")
    await wait_for(lambda: session.status == STATUS_RUNNING)
    with pytest.raises(TurnInProgress):
        manager.start_turn(session, "second")
    gate.release()
    await task
    assert session.status == STATUS_IDLE


async def test_exception_becomes_error_event_and_reopens_provider(manager, provider, spec):
    provider.script = [Raise(RuntimeError("CLI exploded"))]
    session = manager.create(spec)
    await manager.start_turn(session, "boom")
    assert session.status == STATUS_IDLE
    error = [e for e in session.events.replay(0) if e.type == "error"][0]
    assert error.data["message"] == "CLI exploded" and error.data["kind"] == "RuntimeError"
    assert provider.closed == 1 and session.provider_session is None
    provider.script = [result(session_id="cs-2")]
    await manager.start_turn(session, "again")
    assert len(provider.opened) == 2
    assert session.status == STATUS_IDLE


async def test_error_result_drops_provider_and_next_turn_resumes(manager, provider, spec):
    provider.script = [result(session_id="cs-7", is_error=True, result_text="API Error: 401")]
    session = manager.create(spec)
    await manager.start_turn(session, "x")
    assert "error" in types(session)
    assert provider.closed == 1
    provider.script = [result(session_id="cs-7")]
    await manager.start_turn(session, "y")
    assert provider.opened[1].resume == "cs-7"


async def test_approval_allow_flow(manager, provider, spec):
    provider.script = [Ask("Bash", {"command": "ls"}, tool_use_id="tu-1"), result()]
    session = manager.create(spec)
    task = manager.start_turn(session, "run ls")
    await wait_for(lambda: session.status == STATUS_WAITING)
    request = [e for e in session.events.replay(0) if e.type == "permission_request"][0]
    assert request.data["name"] == "Bash" and request.data["tool_use_id"] == "tu-1" and request.data["rendered"] == "ls"
    approval_id = request.data["approval_id"]
    assert approval_id in session.pending
    manager.resolve_approval(session, approval_id, "allow", updated_input={"command": "ls -la"})
    await task
    assert provider.decisions[0].decision == "allow"
    assert provider.decisions[0].updated_input == {"command": "ls -la"}
    resolved = [e for e in session.events.replay(0) if e.type == "permission_resolved"][0]
    assert resolved.data == {"approval_id": approval_id, "decision": "allow", "name": "Bash"}
    assert session.status == STATUS_IDLE and not session.pending


async def test_approval_deny_with_message(manager, provider, spec):
    provider.script = [Ask("Write", {"file_path": "/w/x"}), result()]
    session = manager.create(spec)
    task = manager.start_turn(session, "write")
    await wait_for(lambda: bool(session.pending))
    manager.resolve_approval(session, next(iter(session.pending)), "deny", message="not today")
    await task
    assert provider.decisions[0].decision == "deny" and provider.decisions[0].message == "not today"


async def test_allow_always_adds_exact_rule_and_skips_next_prompt(manager, provider, spec):
    provider.script = [Ask("Bash", {"command": "git status"}), Ask("Bash", {"command": "git status"}), Ask("Bash", {"command": "git push"}), result()]
    session = manager.create(spec)
    task = manager.start_turn(session, "git")
    await wait_for(lambda: bool(session.pending))
    manager.resolve_approval(session, next(iter(session.pending)), "allow_always")
    # second identical call is auto-allowed; third asks again
    await wait_for(lambda: len(session.pending) == 1 and len(provider.decisions) == 2)
    assert [d.decision for d in provider.decisions] == ["allow_always", "allow"]
    pending = [e for e in session.events.replay(0) if e.type == "permission_request"]
    assert len(pending) == 2 and pending[1].data["rendered"] == "git push"
    manager.resolve_approval(session, next(iter(session.pending)), "deny")
    await task
    assert session.policy.to_dict()["allow"] == [{"tool": "Bash", "match": f"^{re.escape('git status')}$"}]


async def test_policy_auto_allow_and_deny(manager, provider, spec):
    provider.script = [Ask("Read", {"file_path": "/w/a"}), Ask("Bash", {"command": "rm -rf /"}), result()]
    session = manager.create(spec, Policy.from_dict({"allow": [{"tool": "Read"}], "deny": [{"tool": "Bash", "match": "rm -rf"}]}))
    await manager.start_turn(session, "go")
    assert [d.decision for d in provider.decisions] == ["allow", "deny"]
    assert "permission_request" not in types(session)
    auto = [e for e in session.events.replay(0) if e.type == "permission_resolved"]
    assert len(auto) == 1 and auto[0].data["auto"] is True and auto[0].data["decision"] == "deny"


async def test_permission_mode_fallbacks(provider, settings):
    manager = SessionManager(provider, settings)
    provider.script = [Ask("Edit", {"file_path": "/w/a"}), Ask("Bash", {"command": "ls"}), result()]
    session = manager.create(SessionSpec(user="u", cwd=settings.workspace, permission_mode="plan"))
    await manager.start_turn(session, "plan")
    assert [d.decision for d in provider.decisions] == ["deny", "deny"]  # plan mode denies every mutating tool
    provider.decisions.clear()
    provider.script = [Ask("Edit", {"file_path": "/w/a"}), result()]
    session = manager.create(SessionSpec(user="u", cwd=settings.workspace, permission_mode="acceptEdits"))
    await manager.start_turn(session, "edit")
    assert [d.decision for d in provider.decisions] == ["allow"]


async def test_approval_timeout_denies(provider, settings):
    settings = type(settings)(**{**settings.__dict__, "approval_timeout_s": 0.05})
    manager = SessionManager(provider, settings)
    provider.script = [Ask("Bash", {"command": "sleep"}), result()]
    session = manager.create(SessionSpec(user="u", cwd=settings.workspace))
    await manager.start_turn(session, "x")
    assert provider.decisions[0].decision == "timeout"
    resolved = [e for e in session.events.replay(0) if e.type == "permission_resolved"][0]
    assert resolved.data["decision"] == "timeout"
    assert session.status == STATUS_IDLE and not session.pending


async def test_interrupt_and_close(manager, provider, spec):
    gate = Gate()
    provider.script = [gate, result()]
    session = manager.create(spec)
    task = manager.start_turn(session, "long")
    await wait_for(lambda: session.status == STATUS_RUNNING)
    await manager.interrupt(session)  # fake releases the gate
    await task
    assert provider.interrupted == 1
    await manager.close(session)
    assert session.status == STATUS_CLOSED and provider.closed == 1 and session.events.closed
    await manager.close(session)  # idempotent


async def test_close_while_waiting_for_approval_denies_it(manager, provider, spec):
    provider.script = [Ask("Bash", {"command": "x"}), result()]
    session = manager.create(spec)
    task = manager.start_turn(session, "x")
    await wait_for(lambda: bool(session.pending))
    await manager.close(session)
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    assert provider.decisions and provider.decisions[0].decision == "deny"
    assert session.status == STATUS_CLOSED
