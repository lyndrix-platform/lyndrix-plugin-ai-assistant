"""Native Claude OAuth login, driven through ``claude setup-token``.

Verified against Claude Code 2.1.301 (see README "Login flow"): the command
refuses to do anything without a TTY (it prints one newline and blocks), so we
run it on a pseudo-terminal. It prints an authorization URL, waits for the
pasted code on stdin ("Paste code here if prompted >"), and on a bad code says
"OAuth error: Invalid code … Press Enter to retry." while staying alive. On
success it prints the long-lived token (``sk-ant-oat01-…``) meant for
``CLAUDE_CODE_OAUTH_TOKEN``.

The bridge keeps exactly one flow at a time, returns the token to the plugin
once, and never logs it. A failed submission ends the flow — the UI simply
starts a new one — because the retry prompt's exact behaviour is not something
we want to depend on.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import json
import os
import pty
import re
import struct
import termios
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Awaitable

from .config import detect_auth

log = logging.getLogger("bridge.auth")

URL_RE = re.compile(r"https://[A-Za-z0-9.\-]+/[^\s\"'<>]*oauth[^\s\"'<>]*")
TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]{20,}")
ANY_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")
ERROR_RE = re.compile(r"(OAuth error[^\n]*|Invalid code[^\n]*|Error:[^\n]*|(?i:failed to [^\n]*|invalid[_ ]grant[^\n]*|expired[^\n]*|unauthori[sz]ed[^\n]*))")
ANSI_RE = re.compile(r"\x1b\[[0-9;?<=>]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|\x1b[=>]|[\x0e\x0f]")
# The ink renderer positions the hint lines after the URL with cursor moves, not
# newlines, so after ANSI stripping the next hint ("Hold Shift while selecting…",
# "Paste code here if prompted >") is glued to the URL. Cut it off there.
URL_TAIL_RE = re.compile(r"(Hold\s*Shift|Paste\s*code|Browser\s*didn|Press\s*Enter).*$", re.IGNORECASE | re.DOTALL)


def clean_url(url: str) -> str:
    """The authorization URL without hint text that the TUI glued onto it."""
    return URL_TAIL_RE.sub("", url).rstrip(".,;:)")

PTY_ROWS, PTY_COLS = 50, 1000  # wide enough that the URL is never wrapped


class AuthFlowError(Exception):
    """The CLI did not behave as expected (no URL, spawn failure …)."""


class AuthFlowPending(AuthFlowError):
    """A flow is already waiting for a code."""


class AuthFlowNotFound(AuthFlowError):
    pass


class AuthFlowNotReady(AuthFlowError):
    """The code was submitted but the CLI has not finished the exchange yet."""


# Cursor movements the TUI uses instead of newlines (cursor down/next line/position/
# erase). Turning them into a line break keeps values from being glued to the text
# the renderer draws on the next line (seen live: the state parameter of the URL and
# the token both got a hint sentence appended).
_CURSOR_BREAK_RE = re.compile(r"\x1b\[[0-9;]*[BEHfdJ]")
_CURSOR_SPACE_RE = re.compile(r"\x1b\[[0-9;]*[CG]")


def strip_ansi(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    text = _CURSOR_BREAK_RE.sub("\n", text)
    text = _CURSOR_SPACE_RE.sub(" ", text)
    return ANSI_RE.sub("", text).replace("\r", "")


def scrub(text: str) -> str:
    """Remove anything token-shaped before text is logged or returned."""
    return ANY_TOKEN_RE.sub("sk-ant-***", text)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class AuthFlow:
    auth_id: str
    started_at: datetime
    expires_at: datetime
    proc: asyncio.subprocess.Process | None = None
    master_fd: int | None = None
    buf: bytearray = field(default_factory=bytearray)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    eof: bool = False
    url: str | None = None
    expiry_task: asyncio.Task[None] | None = None
    exchange_task: asyncio.Task[None] | None = None
    finished: bool = False
    # awaiting_code → exchanging → done | failed. The outcome is kept until the
    # plugin claims it (POST /auth/result) or the flow expires, so a slow
    # exchange never depends on one long HTTP request surviving the proxies.
    phase: str = "awaiting_code"
    result: dict[str, Any] | None = None

    def text(self) -> str:
        return strip_ansi(bytes(self.buf))

    def to_status(self) -> dict[str, Any]:
        out = {
            "auth_id": self.auth_id,
            "started_at": _iso(self.started_at),
            "expires_at": _iso(self.expires_at),
            "url": self.url,
            "phase": self.phase,
            "result_available": self.result is not None,
        }
        if self.result is not None and not self.result.get("ok"):
            out["error"] = self.result.get("error")
        return out


class AuthFlowManager:
    def __init__(
        self,
        cli_path: str | None,
        *,
        expires_in: int = 600,
        url_timeout: float = 45.0,
        submit_timeout: float = 90.0,
        enter_delay: float = 0.4,
        nudge_after: float = 10.0,
        verifier: "Callable[[str], Awaitable[tuple[bool, str | None]]] | None" = None,
    ) -> None:
        self._cli_path = cli_path
        self.expires_in = expires_in
        self.url_timeout = url_timeout
        self.submit_timeout = submit_timeout
        self.enter_delay = enter_delay
        self.nudge_after = nudge_after
        # Validates a parsed token with a real request before it is handed out
        # (default: a one-turn headless `claude -p`); tests inject a stub.
        self._verifier = verifier if verifier is not None else self.verify_token
        self.last_verify: dict[str, Any] | None = None
        self._flow: AuthFlow | None = None
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- public

    @property
    def pending(self) -> AuthFlow | None:
        flow = self._flow
        return flow if flow is not None and not flow.finished else None

    def status(self) -> dict[str, Any]:
        kind, _ = detect_auth()
        flow = self.pending
        out: dict[str, Any] = {"pending": flow is not None, "authenticated": kind, "verified": self.last_verify}
        if flow is not None:
            out.update(flow.to_status())
        return out

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            if self.pending is not None:
                raise AuthFlowPending("a login flow is already pending")
            if not self._cli_path:
                raise AuthFlowError("claude CLI not found in the runner image")
            now = datetime.now(tz=timezone.utc)
            flow = AuthFlow(
                auth_id=uuid.uuid4().hex[:16],
                started_at=now,
                expires_at=now + timedelta(seconds=self.expires_in),
            )
            await self._spawn(flow)
            self._flow = flow
            match = await self._wait_for(flow, lambda t: URL_RE.search(t.replace("\n", "")), self.url_timeout)
            if match is None:
                tail = scrub(flow.text()[-400:])
                await self._terminate(flow)
                raise AuthFlowError(f"claude setup-token printed no authorization URL; output tail: {tail!r}")
            flow.url = clean_url(match.group(0))
            flow.expiry_task = asyncio.create_task(self._expire(flow))
            log.info("login flow started", extra={"ctx": {"auth_id": flow.auth_id}})
            return {
                "auth_id": flow.auth_id,
                "url": flow.url,
                "expires_in": self.expires_in,
                "expires_at": _iso(flow.expires_at),
            }

    async def submit(self, auth_id: str, code: str) -> dict[str, Any]:
        """Hand the code to the CLI and return at once; the exchange runs in the
        background and its outcome is read with :meth:`result` (``status()``
        shows ``phase``/``result_available`` meanwhile)."""
        async with self._lock:
            flow = self._require(auth_id)
            if flow.phase != "awaiting_code" or flow.master_fd is None or flow.url is None:
                raise AuthFlowError(f"flow is not awaiting a code (phase {flow.phase})")
            mark = max(0, len(flow.text()) - 64)
            try:
                # The code alone first: ~90 characters arriving at once are treated as a
                # paste by the input component, and an Enter inside that chunk is swallowed
                # (seen live: the CLI echoed the masked code and waited). Enter follows
                # separately from the exchange task.
                os.write(flow.master_fd, code.strip().encode())
            except OSError as exc:
                await self._terminate(flow)
                raise AuthFlowError(f"could not write code to the CLI: {exc}") from exc
            flow.phase = "exchanging"
            flow.exchange_task = asyncio.create_task(self._exchange(flow, mark), name=f"auth-exchange:{flow.auth_id}")
            return {"accepted": True, "auth_id": flow.auth_id}

    async def _exchange(self, flow: AuthFlow, mark: int) -> None:
        def outcome(text: str) -> re.Match[str] | None:
            tail = text[mark:]
            return TOKEN_RE.search(tail) or ERROR_RE.search(tail)

        await asyncio.sleep(self.enter_delay)
        self._press_enter(flow)
        # No reaction after a while → the Enter was lost (paste detection); press it again once.
        match = await self._wait_for(flow, outcome, min(self.nudge_after, self.submit_timeout))
        if match is None and not flow.finished:
            before = len(flow.text())
            self._press_enter(flow)
            log.info("login flow: no reaction to the code yet, pressed Enter again",
                     extra={"ctx": {"auth_id": flow.auth_id, "output_chars": before}})
            match = await self._wait_for(flow, outcome, max(1.0, self.submit_timeout - self.nudge_after))
        if flow.finished:
            return
        if match is None:
            tail = scrub(flow.text()[mark:][-300:])
            result: dict[str, Any] = {"ok": False, "error": f"no token within {int(self.submit_timeout)} s; output tail: {tail!r}"}
            log.warning("login flow timed out", extra={"ctx": {"auth_id": flow.auth_id, "tail": tail}})
        elif TOKEN_RE.fullmatch(match.group(0)):
            token = match.group(0)
            tail = scrub(flow.text()[mark:][-300:])
            ok, why = await self._verifier(token)
            if ok:
                result = {"ok": True, "token": token}
                log.info("login flow completed", extra={"ctx": {"auth_id": flow.auth_id, "token_length": len(token)}})
            else:
                # The parsed text is not a working token (truncated / glued) — do not store it.
                result = {"ok": False, "error": f"token parsed from the CLI output did not validate: {why}; output tail: {tail!r}"}
                log.warning("login flow: parsed token failed validation",
                            extra={"ctx": {"auth_id": flow.auth_id, "token_length": len(token), "why": why, "tail": tail}})
        else:
            error = scrub(match.group(0)).strip()
            result = {"ok": False, "error": error}
            log.info("login flow failed", extra={"ctx": {"auth_id": flow.auth_id, "error": error}})
        # Stop the CLI first, then publish the outcome: a status that says "done" must
        # never describe a flow whose process is still running. The outcome stays
        # claimable until expiry.
        await self._stop_process(flow)
        if flow.finished:
            return
        flow.result = result
        flow.phase = "done" if result.get("ok") else "failed"

    async def verify_token(self, token: str | None = None, timeout: float = 90.0) -> tuple[bool, str | None]:
        """One tiny headless turn through the CLI: proves the credential works end to
        end. ``token=None`` checks the container's configured credential."""
        if not self._cli_path:
            return False, "claude CLI not found"
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE",)}
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            env.pop("ANTHROPIC_API_KEY", None)
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path, "-p", "Reply with exactly the word OK and nothing else.",
                "--output-format", "json", "--max-turns", "1",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env, cwd=os.environ.get("LYNDRIX_AI_WORKSPACE", "/tmp"),
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            return False, f"no answer within {int(timeout)} s"
        except OSError as exc:
            return False, f"could not run the CLI: {exc}"
        text = scrub(out.decode("utf-8", "replace") + err.decode("utf-8", "replace"))
        try:
            data = json.loads(out.decode("utf-8", "replace") or "{}")
        except ValueError:
            data = {}
        if proc.returncode == 0 and isinstance(data, dict) and not data.get("is_error"):
            return True, None
        detail = (data.get("result") if isinstance(data, dict) else None) or text.strip()[-300:] or f"exit {proc.returncode}"
        return False, str(detail)[:300]

    async def verify_current(self) -> dict[str, Any]:
        ok, why = await self.verify_token(None)
        self.last_verify = {"ok": ok, "error": why, "at": _iso(datetime.now(tz=timezone.utc))}
        return self.last_verify

    @staticmethod
    def _press_enter(flow: AuthFlow) -> None:
        if flow.master_fd is None:
            return
        try:
            os.write(flow.master_fd, b"\r")
        except OSError:
            pass

    async def result(self, auth_id: str) -> dict[str, Any]:
        """The outcome of the exchange — the token is handed out exactly once."""
        async with self._lock:
            flow = self._require(auth_id)
            if flow.result is None:
                raise AuthFlowNotReady(auth_id)
            res = flow.result
            await self._terminate(flow)       # drops the flow and the token copy
            return res

    async def cancel(self, auth_id: str | None = None) -> bool:
        flow = self.pending
        if flow is None or (auth_id is not None and flow.auth_id != auth_id):
            return False
        await self._terminate(flow)
        log.info("login flow cancelled", extra={"ctx": {"auth_id": flow.auth_id}})
        return True

    # -------------------------------------------------------------- internals

    def _require(self, auth_id: str) -> AuthFlow:
        flow = self.pending
        if flow is None or flow.auth_id != auth_id:
            raise AuthFlowNotFound(auth_id)
        return flow

    async def _spawn(self, flow: AuthFlow) -> None:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
        env = {
            **os.environ,
            "TERM": "xterm-256color",
            "COLUMNS": str(PTY_COLS),
            "LINES": str(PTY_ROWS),
            "NO_COLOR": "1",
        }
        try:
            flow.proc = await asyncio.create_subprocess_exec(
                self._cli_path or "claude",
                "setup-token",
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
            )
        except Exception as exc:
            os.close(master)
            os.close(slave)
            raise AuthFlowError(f"could not start claude setup-token: {exc}") from exc
        finally:
            try:
                os.close(slave)
            except OSError:
                pass
        os.set_blocking(master, False)
        flow.master_fd = master
        asyncio.get_running_loop().add_reader(master, self._on_readable, flow)

    def _on_readable(self, flow: AuthFlow) -> None:
        if flow.master_fd is None:
            return
        try:
            chunk = os.read(flow.master_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            asyncio.get_running_loop().remove_reader(flow.master_fd)
            flow.eof = True
        else:
            flow.buf += chunk
        flow.changed.set()

    async def _wait_for(
        self, flow: AuthFlow, predicate: Callable[[str], re.Match[str] | None], timeout: float
    ) -> re.Match[str] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            match = predicate(flow.text())
            if match is not None:
                return match
            if flow.eof:
                return None
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(flow.changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            flow.changed.clear()

    async def _expire(self, flow: AuthFlow) -> None:
        await asyncio.sleep(self.expires_in)
        if not flow.finished:
            log.info("login flow expired", extra={"ctx": {"auth_id": flow.auth_id}})
            await self._terminate(flow)

    async def _terminate(self, flow: AuthFlow) -> None:
        if flow.finished:
            return
        flow.finished = True
        flow.result = None
        if flow.expiry_task is not None and flow.expiry_task is not asyncio.current_task():
            flow.expiry_task.cancel()
        if flow.exchange_task is not None and flow.exchange_task is not asyncio.current_task():
            flow.exchange_task.cancel()
        await self._stop_process(flow)
        if self._flow is flow:
            self._flow = None

    async def _stop_process(self, flow: AuthFlow) -> None:
        """Stop the CLI and drop its output buffer (the flow object stays)."""
        if flow.master_fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(flow.master_fd)
            except Exception:  # noqa: BLE001
                pass
            try:
                os.close(flow.master_fd)
            except OSError:
                pass
            flow.master_fd = None
        proc = flow.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        flow.buf = bytearray()
