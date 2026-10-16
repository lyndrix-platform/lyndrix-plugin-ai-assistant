from claude_agent_sdk import StreamEvent, SystemMessage, ThinkingBlock

from bridge.claude_provider import TranslationState, result_error_text, translate

from .conftest import assistant, result, text, tool_result, tool_use


def _delta(kind: str, **payload):
    return StreamEvent(uuid="u", session_id="s", event={"type": "content_block_delta", "index": 0, "delta": {"type": kind, **payload}})


def test_text_deltas_stream_and_suppress_whole_text():
    state = TranslationState()
    state.begin_turn()
    events = translate(_delta("text_delta", text="Hel"), state) + translate(_delta("text_delta", text="lo"), state)
    assert events == [("assistant_text", {"text": "Hel", "delta": True}), ("assistant_text", {"text": "lo", "delta": True})]
    # the complete message must not repeat the text, but tool uses are still emitted
    events = translate(assistant(text("Hello"), tool_use("t1", "Read", file_path="/workspace/a.py")), state)
    assert [e[0] for e in events] == ["tool_use"]
    assert events[0][1] == {"tool_use_id": "t1", "name": "Read", "input": {"file_path": "/workspace/a.py"}}
    # ...and the next message (no deltas) falls back to whole text
    events = translate(assistant(text("Second")), state)
    assert events == [("assistant_text", {"text": "Second", "delta": False})]


def test_whole_text_without_streaming():
    state = TranslationState()
    assert translate(assistant(text("plain")), state) == [("assistant_text", {"text": "plain", "delta": False})]


def test_thinking_blocks_and_deltas():
    state = TranslationState()
    assert translate(_delta("thinking_delta", thinking="hm"), state) == [("thinking", {"text": "hm", "delta": True})]
    assert translate(assistant(ThinkingBlock(thinking="hm", signature="sig")), state) == []
    assert translate(assistant(ThinkingBlock(thinking="again", signature="sig")), state) == [
        ("thinking", {"text": "again", "delta": False})
    ]


def test_subagent_stream_events_are_ignored():
    state = TranslationState()
    ev = StreamEvent(uuid="u", session_id="s", parent_tool_use_id="parent", event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}})
    assert translate(ev, state) == []


def test_tool_result_gets_tool_name_and_is_truncated():
    state = TranslationState()
    translate(assistant(tool_use("t1", "Bash", command="ls")), state)
    events = translate(tool_result("t1", "x" * 10_000, is_error=False), state)
    assert len(events) == 1 and events[0][0] == "tool_result"
    data = events[0][1]
    assert data["name"] == "Bash" and data["is_error"] is False and data["tool_use_id"] == "t1"
    assert len(data["output"].encode()) <= 4096


def test_result_success():
    state = TranslationState()
    events = translate(result(session_id="abc", cost=0.5), state)
    assert len(events) == 1
    kind, data = events[0]
    assert kind == "result"
    assert data["claude_session_id"] == "abc" and data["total_cost_usd"] == 0.5 and data["is_error"] is False
    assert state.claude_session_id == "abc"


def test_result_error_emits_error_event():
    state = TranslationState()
    events = translate(result(is_error=True, errors=[], result_text="API Error: 401 authentication_error"), state)
    assert [e[0] for e in events] == ["result", "error"]
    assert "401" in events[1][1]["message"]


def test_result_error_text_pick_order():
    assert result_error_text(result(is_error=True, errors=["boom"], result_text="x")) == "boom"
    assert result_error_text(result(is_error=True, result_text="API Error")) == "API Error"
    assert result_error_text(result(is_error=True, subtype="error_max_turns")) == "error_max_turns"
    assert result_error_text(result(is_error=True)) == "unknown error"


def test_assistant_error_is_not_duplicated_by_result():
    state = TranslationState()
    state.begin_turn()
    msg = assistant(text(""))
    msg.error = "authentication_failed"
    events = translate(msg, state)
    assert events == [("error", {"message": "Claude API error: authentication_failed", "kind": "assistant_error"})]
    events = translate(result(is_error=True, result_text="API Error: 401"), state)
    assert [e[0] for e in events] == ["result"]


def test_init_records_claude_session_id():
    state = TranslationState()
    assert translate(SystemMessage(subtype="init", data={"session_id": "sess-9"}), state) == []
    assert state.claude_session_id == "sess-9"
