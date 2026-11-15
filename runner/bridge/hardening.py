"""Keep the bridge's secrets away from the agent it runs (AI-01, AI-03).

The bridge, every Claude Code process and every command those run (the agent's
Bash) share one uid in the runner container. Without care, one tool call reads
the bridge token from ``/proc/<bridge pid>/environ`` or from its own environment
(the SDK starts the CLI with ``{**os.environ, **options.env}``), and with it
approves its own tool calls, switches sessions to bypass and reads every other
user's session. So the bridge

* takes its secrets out of ``os.environ`` once at start-up (:func:`scrub_environ`).
  Child processes then only get what the bridge hands them explicitly — the CLI
  its Claude credential and the agent's Lyndrix key, never the bridge token;
* marks itself non-dumpable (:func:`make_undumpable`): ``/proc/<pid>/environ``,
  ``/proc/<pid>/mem`` and ``ptrace`` of the bridge then need ``CAP_SYS_PTRACE``,
  which no process in the container has. Children are dumpable again after
  ``execve``; that is fine, their environment holds no bridge secret;
* refuses requests that come from inside the container (:func:`is_local_peer`):
  the agent shares the bridge's network namespace, the plugin never does. Only
  the token-free liveness probe ``/livez`` answers locally.

What this does NOT protect: the Claude credential and the agent's Lyndrix key
are in every CLI process's environment by necessity, so every session can read
them. Keeping those out needs an auth-injecting proxy and per-user uids — see
the plugin's fix report.
"""

from __future__ import annotations

import ctypes
import ipaddress
import logging
from pathlib import Path
from typing import MutableMapping

log = logging.getLogger("bridge.hardening")

# Only the bridge may ever hold these.
BRIDGE_ONLY_ENV = ("LYNDRIX_AI_BRIDGE_TOKEN",)
# What the Claude Code CLI needs to reach Claude (exactly one is set by the plugin).
CREDENTIAL_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
# The agent's own Lyndrix account (setting agent_lyndrix_access).
AGENT_ENV = ("LYNDRIX_API_URL", "LYNDRIX_API_KEY")
SCRUBBED_ENV = BRIDGE_ONLY_ENV + CREDENTIAL_ENV + AGENT_ENV

_PR_SET_DUMPABLE = 4
_PR_GET_DUMPABLE = 3


def scrub_environ(environ: MutableMapping[str, str]) -> list[str]:
    """Remove the bridge's secrets from ``environ`` (normally ``os.environ``, which
    also unsets them for every child process started afterwards). Read them into
    :class:`bridge.config.Settings` first. Returns the names that were removed."""
    removed = [key for key in SCRUBBED_ENV if key in environ]
    for key in removed:
        del environ[key]
    return removed


def make_undumpable() -> bool:
    """``prctl(PR_SET_DUMPABLE, 0)`` for this process. ``False`` when not possible
    (non-Linux, no libc) — the caller logs it; the bridge still runs."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            return False
        return libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def _normalise(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("::ffff:"):          # IPv4-mapped IPv6 peer
        host = host[len("::ffff:"):]
    return host


def local_addresses(proc_net: Path = Path("/proc/net")) -> frozenset[str]:
    """Every address of this network namespace (= the container): loopback plus the
    IPv4 entries the kernel marks ``/32 host LOCAL`` in ``fib_trie`` and the IPv6
    addresses of ``if_inet6``."""
    addrs = {"127.0.0.1", "::1", "localhost"}
    try:
        lines = (proc_net / "fib_trie").read_text().splitlines()
        for prev, line in zip(lines, lines[1:]):
            if "/32 host LOCAL" in line:
                addrs.add(prev.strip().split()[-1])
    except (OSError, IndexError):
        pass
    try:
        for line in (proc_net / "if_inet6").read_text().splitlines():
            parts = line.split()
            if parts:
                addrs.add(str(ipaddress.IPv6Address(int(parts[0], 16))))
    except (OSError, ValueError):
        pass
    return frozenset(_normalise(a) for a in addrs)


def is_local_peer(host: str | None, local: frozenset[str]) -> bool:
    """True when a request comes from inside this container (loopback or one of its
    own addresses). An unknown peer (``None``) counts as local: refuse rather than guess."""
    if not host:
        return True
    host = _normalise(host)
    if host in local:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False                         # e.g. Starlette's TestClient ("testclient")
