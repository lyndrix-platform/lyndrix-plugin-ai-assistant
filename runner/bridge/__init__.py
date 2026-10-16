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

__version__ = "0.1.0"
