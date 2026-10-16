"""Per-session event log: a ring buffer plus fan-out to live SSE readers.

Why a ring buffer and not a database: the plugin is the system of record
(``ai_messages``); the bridge only needs to survive the plugin's SSE proxy
reconnecting and asking ``?after=<seq>``. 500 events comfortably cover one
long agentic turn.

Also home to the summarisation helpers that keep tool inputs/outputs small
enough to travel over SSE and sit in a chat UI.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator

MAX_INPUT_BYTES = 2048
MAX_OUTPUT_BYTES = 4096
TRUNCATION_MARK = "… [truncated]"

# Tool-input keys the UI needs verbatim (file paths, commands). They are kept
# whole while the remaining keys share what is left of the byte budget.
PATH_LIKE_KEYS = frozenset(
    {"file_path", "path", "notebook_path", "cwd", "directory", "pattern", "glob", "url", "command"}
)


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class Event:
    """One bridge event; ``seq`` is strictly increasing per session."""

    seq: int
    ts: str
    type: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "ts": self.ts, "type": self.type, "data": self.data}

    def to_sse(self) -> str:
        return f"id: {self.seq}\ndata: {json.dumps(self.to_dict(), default=str)}\n\n"


class EventLog:
    """Ring buffer of :class:`Event` with live subscribers."""

    def __init__(self, maxlen: int = 500) -> None:
        self._buf: deque[Event] = deque(maxlen=maxlen)
        self._seq = 0
        self._subscribers: set[asyncio.Queue[Event | None]] = set()
        self._closed = False

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def closed(self) -> bool:
        return self._closed

    def append(self, type_: str, data: dict[str, Any] | None = None) -> Event:
        self._seq += 1
        event = Event(seq=self._seq, ts=utc_now_iso(), type=type_, data=data or {})
        self._buf.append(event)
        for queue in list(self._subscribers):
            queue.put_nowait(event)
        return event

    def replay(self, after: int = 0) -> list[Event]:
        return [event for event in self._buf if event.seq > after]

    def last(self, n: int) -> list[Event]:
        if n <= 0:
            return []
        return list(self._buf)[-n:]

    def close(self) -> None:
        """Wake every live reader so its stream can end."""
        self._closed = True
        for queue in list(self._subscribers):
            queue.put_nowait(None)

    async def stream(self, after: int = 0, keepalive: float = 15.0) -> AsyncIterator[Event | None]:
        """Yield stored events after ``after`` and then live ones; ``None`` = keep-alive tick.

        The subscription is registered *before* the replay snapshot is taken,
        so an event appended in between is neither lost nor duplicated (live
        items with ``seq <= last`` are skipped).
        """
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            last = after
            for event in self.replay(after):
                last = event.seq
                yield event
            if self._closed:
                return
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=keepalive)
                except asyncio.TimeoutError:
                    yield None
                    continue
                if item is None:
                    return
                if item.seq <= last:
                    continue
                last = item.seq
                yield item
        finally:
            self._subscribers.discard(queue)


# --------------------------------------------------------------------------- #
# Summarisation helpers
# --------------------------------------------------------------------------- #


def truncate_text(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes (marker included)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    mark = TRUNCATION_MARK.encode("utf-8")
    keep = max(0, limit - len(mark))
    return encoded[:keep].decode("utf-8", errors="ignore") + TRUNCATION_MARK


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)


def summarise_input(value: Any, limit: int = MAX_INPUT_BYTES) -> dict[str, Any]:
    """Shrink a tool input to roughly ``limit`` bytes, keeping path-like keys whole.

    Non-dict inputs are wrapped as ``{"value": ...}`` so the event shape stays
    stable for the UI.
    """
    if not isinstance(value, dict):
        return {"value": truncate_text(_as_text(value), limit)}

    out: dict[str, Any] = {}
    budget = limit
    for key, item in value.items():
        if key in PATH_LIKE_KEYS and isinstance(item, str):
            out[key] = truncate_text(item, limit)
            budget -= len(out[key].encode("utf-8"))

    rest = [key for key in value if key not in out]
    per_key = max(64, budget // len(rest)) if rest else 0
    for key in rest:
        item = value[key]
        text = _as_text(item)
        if len(text.encode("utf-8")) > per_key:
            out[key] = truncate_text(text, per_key)
        else:
            out[key] = item
    return out


def tool_result_text(content: Any) -> str:
    """Flatten a tool_result ``content`` (str | list of blocks | None) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(_as_text(block))
            else:
                parts.append(_as_text(block))
        return "\n".join(parts)
    return _as_text(content)
