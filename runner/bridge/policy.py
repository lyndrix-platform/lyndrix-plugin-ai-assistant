"""Per-session tool-approval guardrails ("auto mode").

The operator (or the user, via the plugin UI) hands each session a policy of
allow / ask / deny rules. Every rule names a tool (or ``*``) and optionally a
regex that is tested against a *textual rendering* of the tool input — the
command for Bash, the file path for file tools, the URL for web tools. Keeping
the rendering here (and not in the UI) means the UI only ever shows the same
string the policy matched against.

Evaluation order is deny → allow → ask; an unmatched call falls back to the
session's Claude Code ``permission_mode``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

Verdict = Literal["allow", "ask", "deny"]

FILE_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
MUTATING_TOOLS = FILE_EDIT_TOOLS | frozenset({"Bash"})

# Which input key carries the human-meaningful part of each tool call.
_RENDER_KEYS: dict[str, tuple[str, ...]] = {
    "Bash": ("command",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "Write": ("file_path",),
    "Read": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("pattern", "path"),
    "Grep": ("pattern", "path"),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
    "Agent": ("description",),
    "Task": ("description",),
    "Skill": ("skill", "name"),
}


class PolicyError(ValueError):
    """Raised for a malformed policy (unknown shape or invalid regex)."""


def render_input(tool_name: str, tool_input: Any) -> str:
    """Return the string a policy ``match`` is tested against."""
    if not isinstance(tool_input, dict):
        return tool_input if isinstance(tool_input, str) else json.dumps(tool_input, default=str)
    keys = _RENDER_KEYS.get(tool_name)
    if keys:
        parts = [str(tool_input[key]) for key in keys if tool_input.get(key) not in (None, "")]
        if parts:
            return " ".join(parts)
    # MCP tools and unknown tools: a compact, key-sorted JSON rendering so a
    # regex can still target a field ("\"path\": \"/etc").
    return json.dumps(tool_input, sort_keys=True, default=str, ensure_ascii=False)


@dataclass(frozen=True)
class PolicyRule:
    tool: str
    match: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tool, str) or not self.tool.strip():
            raise PolicyError("rule.tool must be a non-empty string")
        if self.match is not None:
            try:
                re.compile(self.match)
            except re.error as exc:
                raise PolicyError(f"invalid regex for tool {self.tool!r}: {exc}") from exc

    def matches(self, tool_name: str, rendered: str) -> bool:
        if self.tool != "*" and self.tool != tool_name:
            return False
        if self.match is None:
            return True
        return re.search(self.match, rendered, flags=re.DOTALL) is not None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tool": self.tool}
        if self.match is not None:
            out["match"] = self.match
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> "PolicyRule":
        if isinstance(raw, str):
            return cls(tool=raw)
        if not isinstance(raw, dict):
            raise PolicyError("rule must be an object {tool, match?}")
        match = raw.get("match")
        if match is not None and not isinstance(match, str):
            raise PolicyError("rule.match must be a string")
        return cls(tool=str(raw.get("tool", "")), match=match)


@dataclass
class Policy:
    allow: list[PolicyRule] = field(default_factory=list)
    ask: list[PolicyRule] = field(default_factory=list)
    deny: list[PolicyRule] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Any) -> "Policy":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise PolicyError("policy must be an object with allow/ask/deny lists")
        unknown = set(raw) - {"allow", "ask", "deny"}
        if unknown:
            raise PolicyError(f"unknown policy keys: {sorted(unknown)}")

        def rules(key: str) -> list[PolicyRule]:
            items = raw.get(key) or []
            if not isinstance(items, list):
                raise PolicyError(f"policy.{key} must be a list")
            return [PolicyRule.from_dict(item) for item in items]

        return cls(allow=rules("allow"), ask=rules("ask"), deny=rules("deny"))

    def to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "allow": [rule.to_dict() for rule in self.allow],
            "ask": [rule.to_dict() for rule in self.ask],
            "deny": [rule.to_dict() for rule in self.deny],
        }

    def evaluate(self, tool_name: str, tool_input: Any) -> Verdict | None:
        """deny → allow → ask; ``None`` when nothing matched."""
        rendered = render_input(tool_name, tool_input)
        for verdict, rules in (("deny", self.deny), ("allow", self.allow), ("ask", self.ask)):
            if any(rule.matches(tool_name, rendered) for rule in rules):
                return verdict  # type: ignore[return-value]
        return None

    def allow_exact(self, tool_name: str, tool_input: Any) -> PolicyRule:
        """Add an allow rule for exactly this call (what ``allow_always`` means here)."""
        rendered = render_input(tool_name, tool_input)
        rule = PolicyRule(tool=tool_name, match=f"^{re.escape(rendered)}$")
        if rule not in self.allow:
            self.allow.append(rule)
        return rule


def fallback_verdict(permission_mode: str, tool_name: str) -> Verdict:
    """What an unmatched call gets, mirroring Claude Code's own modes.

    Claude Code already applies most of this before it consults the callback
    (acceptEdits never asks for edits, bypassPermissions never asks at all),
    so this mostly matters for ``default`` and ``plan``.
    """
    if permission_mode == "bypassPermissions":
        return "allow"
    if permission_mode == "acceptEdits":
        return "allow" if tool_name in FILE_EDIT_TOOLS else "ask"
    if permission_mode == "plan":
        return "deny" if tool_name in MUTATING_TOOLS else "ask"
    return "ask"
