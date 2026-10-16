"""Lyndrix AI runner bridge.

A small FastAPI service that lives next to Claude Code inside the
``lyndrix-ai-runner`` container. The Lyndrix AI-assistant plugin drives Claude
Code sessions through it over HTTP (JSON + Server-Sent Events) and answers the
interactive tool approvals Claude Code asks for.

Modules, one per concern:

- :mod:`bridge.config`           — settings from environment variables
- :mod:`bridge.events`           — per-session event ring buffer + SSE fan-out
- :mod:`bridge.claude_provider`  — ``claude_agent_sdk`` wrapper and message translation
- :mod:`bridge.sessions`         — session registry, turn runner, approval futures
- :mod:`bridge.app`              — HTTP contract (routes, auth, SSE)
"""

from pathlib import Path as _Path


def _read_version() -> str:
    """Single source of truth is ``runner/VERSION`` (copied to ``/opt/bridge/VERSION``
    in the image); the fallback only matters for odd checkouts."""
    for candidate in (_Path(__file__).resolve().parent.parent / "VERSION", _Path("/opt/bridge/VERSION")):
        try:
            text = candidate.read_text().strip()
        except OSError:
            continue
        if text:
            return text
    return "0.0.0"


__version__ = _read_version()
