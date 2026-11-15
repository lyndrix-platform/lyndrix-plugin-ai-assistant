"""Redaction of credential-shaped strings before a transcript is stored (AI-12).

Tool output lands in ``ai_messages`` for good: an ``env`` or ``cat .env`` in one
turn used to keep a Claude token, a Lyndrix key or a Git token readable for every
plugin admin forever. Only well-known token *formats* are replaced — generic
"password=…" heuristics would mangle ordinary text — so this complements the
built-in guardrails, it does not make storing secrets safe.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),                        # Anthropic API keys / OAuth tokens
    re.compile(r"\blyk_[A-Za-z0-9_\-]{16,}"),                          # Lyndrix API keys and sessions
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"),                        # GitLab personal access tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),                       # GitHub tokens
    re.compile(r"\bhv[sb]\.[A-Za-z0-9_\-]{20,}"),                      # Vault tokens
    re.compile(r"(?i)(?<=\bbearer )[A-Za-z0-9._~+/=\-]{16,}"),         # Authorization: Bearer <token>
    re.compile(r"(?i)(?<=authorization: basic )[A-Za-z0-9+/=]{12,}"),  # Authorization: Basic <b64>
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"),
)


def redact(text: Any) -> Any:
    """``text`` with every credential-shaped substring replaced; non-strings unchanged."""
    if not isinstance(text, str) or not text:
        return text
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_obj(value: Any) -> Any:
    """Recursive :func:`redact` for the JSON ``meta`` of a transcript entry."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value
