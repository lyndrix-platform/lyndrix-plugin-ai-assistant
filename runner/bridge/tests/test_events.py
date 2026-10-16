import asyncio
import json

from bridge.events import EventLog, summarise_input, tool_result_text, truncate_text


def test_ring_buffer_and_seq():
    log = EventLog(maxlen=3)
    for i in range(5):
        log.append("status", {"i": i})
    assert log.last_seq == 5
    assert [e.seq for e in log.replay(0)] == [3, 4, 5]
    assert [e.seq for e in log.replay(4)] == [5]
    assert [e.data["i"] for e in log.last(2)] == [3, 4]


def test_sse_format():
    log = EventLog()
    event = log.append("assistant_text", {"text": "hi", "delta": True})
    sse = event.to_sse()
    assert sse.startswith("id: 1\ndata: ")
    payload = json.loads(sse.split("data: ", 1)[1].strip())
    assert payload == {"seq": 1, "ts": event.ts, "type": "assistant_text", "data": {"text": "hi", "delta": True}}


async def test_stream_replays_then_lives_without_gaps_or_dupes():
    log = EventLog()
    log.append("a")
    log.append("b")
    seen: list[int] = []

    async def consume():
        async for item in log.stream(after=1, keepalive=0.05):
            if item is None:
                continue
            seen.append(item.seq)
            if item.seq >= 4:
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    log.append("c")
    log.append("d")
    await asyncio.wait_for(task, 2)
    assert seen == [2, 3, 4]


async def test_stream_emits_keepalive_and_ends_on_close():
    log = EventLog()
    items = []

    async def consume():
        async for item in log.stream(after=0, keepalive=0.02):
            items.append(item)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.08)
    log.close()
    await asyncio.wait_for(task, 1)
    assert items and all(item is None for item in items)


def test_truncate_text_is_byte_bounded():
    out = truncate_text("ä" * 5000, 100)
    assert len(out.encode()) <= 100
    assert out.endswith("[truncated]")
    assert truncate_text("short", 100) == "short"


def test_summarise_input_keeps_paths_and_shrinks_the_rest():
    path = "/workspace/" + "deep/" * 50 + "file.py"
    out = summarise_input({"file_path": path, "content": "x" * 10_000, "n": 3}, limit=2048)
    assert out["file_path"] == path
    assert out["n"] == 3
    assert len(out["content"].encode()) < 2048
    assert len(json.dumps(out).encode()) < 2048 + 512


def test_summarise_input_non_dict():
    assert summarise_input("raw")["value"] == "raw"


def test_tool_result_text_flattens_blocks():
    assert tool_result_text(None) == ""
    assert tool_result_text("plain") == "plain"
    assert tool_result_text([{"type": "text", "text": "a"}, {"type": "image", "data": "..."}]) == "a\n[image]"
