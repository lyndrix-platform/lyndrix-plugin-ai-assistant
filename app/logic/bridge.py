"""HTTP client for the bridge inside the runner container.

The bridge (``runner/bridge``) drives Claude Code through the Agent SDK and
speaks a small REST + SSE API protected by a shared secret. This client is the
only place that knows its URL and header; the plugin API proxies the calls so
the browser never talks to the runner directly.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Optional

import httpx

from core.api import get_logger

log = get_logger("Plugin:AI:Bridge")

HEADER = "X-Bridge-Token"


class BridgeError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"bridge {status}: {detail}")
        self.status = status
        self.detail = detail


class BridgeClient:
    def __init__(self, base_url: str, token: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {HEADER: token}
        self._timeout = timeout

    def _client(self, timeout: Optional[float] = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, headers=self._headers, timeout=timeout or self._timeout)

    async def _call(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
                    timeout: Optional[float] = None) -> Any:
        try:
            async with self._client(timeout) as c:
                r = await c.request(method, path, json=json, params=params)
        except httpx.TimeoutException as exc:
            raise BridgeError(504, f"runner did not answer within {timeout or self._timeout:.0f} s") from exc
        except httpx.HTTPError as exc:   # connect errors while the container is (re)starting
            raise BridgeError(503, f"runner unreachable: {exc}") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise BridgeError(r.status_code, str(detail)[:500])
        return r.json() if r.content else None

    # ------------------------------------------------------------------ calls

    async def health(self) -> dict:
        return await self._call("GET", "/health")

    async def create_session(self, payload: dict) -> dict:
        return await self._call("POST", "/sessions", json=payload)

    async def list_sessions(self) -> list[dict]:
        data = await self._call("GET", "/sessions")
        return data if isinstance(data, list) else (data or {}).get("sessions", [])

    async def get_session(self, session_id: str) -> dict:
        return await self._call("GET", f"/sessions/{session_id}")

    async def send_message(self, session_id: str, content: str) -> dict:
        return await self._call("POST", f"/sessions/{session_id}/messages", json={"content": content})

    async def approve(self, session_id: str, approval_id: str, decision: str,
                      updated_input: dict | None = None, message: str | None = None) -> dict:
        body: dict[str, Any] = {"decision": decision}
        if updated_input is not None:
            body["updated_input"] = updated_input
        if message:
            body["message"] = message
        return await self._call("POST", f"/sessions/{session_id}/approvals/{approval_id}", json=body)

    async def set_permission_mode(self, session_id: str, mode: str) -> dict:
        return await self._call("POST", f"/sessions/{session_id}/permission-mode", json={"permission_mode": mode})

    async def interrupt(self, session_id: str) -> dict:
        return await self._call("POST", f"/sessions/{session_id}/interrupt")

    async def close_session(self, session_id: str) -> None:
        await self._call("DELETE", f"/sessions/{session_id}")

    async def stream_events(self, session_id: str, after: int = 0) -> AsyncIterator[bytes]:
        """Raw SSE bytes of the session's event stream (re-emitted 1:1 by the API)."""
        async with self._client(timeout=httpx.Timeout(10.0, read=None)) as c:
            async with c.stream("GET", f"/sessions/{session_id}/events", params={"after": after}) as r:
                if r.status_code >= 400:
                    body = await r.aread()
                    raise BridgeError(r.status_code, body.decode("utf-8", "replace")[:300])
                async for chunk in r.aiter_bytes():
                    yield chunk

    async def iter_events(self, session_id: str, after: int = 0) -> AsyncIterator[dict]:
        """Parsed events (``{seq, ts, type, data}``) — used by the transcript recorder."""
        import json

        buffer = ""
        async for chunk in self.stream_events(session_id, after):
            buffer += chunk.decode("utf-8", "replace")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                for line in frame.splitlines():
                    if line.startswith("data:"):
                        try:
                            yield json.loads(line[5:].strip())
                        except ValueError:
                            continue
