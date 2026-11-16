"""Per-session tool-approval guardrails ("auto mode").

The operator (or the user, via the plugin UI) hands each session a policy of
allow / ask / deny rules. Every rule names a tool (or ``*``) and optionally a
regex that is tested against a *textual rendering* of the tool input — the
command for Bash, the file path for file tools, the URL for web tools. Keeping
the rendering here (and not in the UI) means the UI only ever shows the same
string the policy matched against.

Evaluation order is built-in deny → deny → allow → ask; an unmatched call falls
back to the session's Claude Code ``permission_mode``.

Matching (AI-07):

* deny and ask rules match anywhere in the rendering (``re.search``), also after
  whitespace is collapsed — ``rm  -rf`` does not slip past a ``rm -rf`` rule;
* allow rules match from the start (``re.match``), and never a Bash command that
  chains, substitutes or redirects (``; & | ` $( < >`` or a line break): an
  allow rule ``^git status`` must not carry ``git status; curl … $(env)``;
* "allow always" adds an *exact* rule (whole string, ``\Z``), which may contain
  shell syntax because nothing can be appended to it.

The built-in deny rules apply in every mode, bypassPermissions included. They
are seatbelts against the commonest secret grabs of an injected prompt (dumping
the environment, reading ``/proc/*/environ``, naming the credential variables,
writing git internals) — not a boundary a determined user cannot rewrite around.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Literal

Verdict = Literal["allow", "ask", "deny"]

FILE_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
MUTATING_TOOLS = FILE_EDIT_TOOLS | frozenset({"Bash"})

# Shell syntax that chains, substitutes or redirects.
_SHELL_CONTROL_RE = re.compile(r"[;&|`<>\n\r]|\$\(")
_WHITESPACE_RE = re.compile(r"\s+")
# Tools whose rendering is one file path: matched also in normalised form.
PATH_TOOLS = frozenset({"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"})


def normalise_path(path: str) -> str:
    """Lexical normal form: no ``.``/``..`` segments, no repeated slashes (``posixpath``
    keeps a leading ``//``; the kernel does not care, so neither do we)."""
    if not path:
        return path
    norm = posixpath.normpath(path)
    return "/" + norm.lstrip("/") if norm.startswith("//") else norm

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
    # "allow always": the rendering must equal the recorded one (see Policy.allow_exact).
    exact: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tool, str) or not self.tool.strip():
            raise PolicyError("rule.tool must be a non-empty string")
        if self.match is not None:
            try:
                re.compile(self.match)
            except re.error as exc:
                raise PolicyError(f"invalid regex for tool {self.tool!r}: {exc}") from exc

    def _tool_matches(self, tool_name: str) -> bool:
        return self.tool == "*" or self.tool == tool_name

    def matches(self, tool_name: str, rendered: str) -> bool:
        """deny/ask semantics: the regex anywhere, raw or whitespace-collapsed."""
        if not self._tool_matches(tool_name):
            return False
        if self.match is None:
            return True
        if re.search(self.match, rendered, flags=re.DOTALL) is not None:
            return True
        collapsed = _WHITESPACE_RE.sub(" ", rendered)
        return collapsed != rendered and re.search(self.match, collapsed, flags=re.DOTALL) is not None

    def allows(self, tool_name: str, rendered: str) -> bool:
        """allow semantics: from the start; an exact rule must cover the whole string,
        any other rule never a Bash command with shell control syntax."""
        if not self._tool_matches(tool_name):
            return False
        if self.match is None:
            return True
        if self.exact:
            return re.fullmatch(self.match, rendered, flags=re.DOTALL) is not None
        if tool_name == "Bash" and _SHELL_CONTROL_RE.search(rendered):
            return False
        return re.match(self.match, rendered, flags=re.DOTALL) is not None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tool": self.tool}
        if self.match is not None:
            out["match"] = self.match
        if self.exact:
            out["exact"] = True
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
        return cls(tool=str(raw.get("tool", "")), match=match, exact=bool(raw.get("exact", False)))


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
        """built-in deny → deny → allow → ask; ``None`` when nothing matched.

        File paths: deny/ask rules also see the normalised path (``/w/./.claude``,
        ``//proc/self/environ``, ``/w/docs/../.claude`` cannot slip past them), and an
        allow rule only covers a path that is already in normal form — otherwise
        ``^/w/docs/`` would allow ``/w/docs/../.claude/settings.json``."""
        rendered = render_input(tool_name, tool_input)
        variants = [rendered]
        normal = True
        if tool_name in PATH_TOOLS:
            norm = normalise_path(rendered)
            if norm != rendered:
                variants.append(norm)
                normal = False

        def hit(rules: list[PolicyRule] | tuple[PolicyRule, ...]) -> bool:
            return any(rule.matches(tool_name, v) for rule in rules for v in variants)

        if hit(BUILTIN_DENY) or hit(self.deny):
            return "deny"
        if normal and any(rule.allows(tool_name, rendered) for rule in self.allow):
            return "allow"
        if hit(self.ask):
            return "ask"
        return None

    def enforced(self, tool_name: str, tool_input: Any) -> Verdict | None:
        """What the PreToolUse hook enforces for EVERY call, in every mode — also the
        calls the CLI would allow without asking (read-only tools, acceptEdits,
        bypassPermissions): ``deny`` and ``ask``. ``None`` leaves the call to the CLI's
        own flow (which consults ``can_use_tool`` where it asks)."""
        verdict = self.evaluate(tool_name, tool_input)
        return verdict if verdict in ("deny", "ask") else None

    def allow_exact(self, tool_name: str, tool_input: Any) -> PolicyRule:
        """Add an allow rule for exactly this call (what ``allow_always`` means here)."""
        rendered = render_input(tool_name, tool_input)
        rule = PolicyRule(tool=tool_name, match=f"{re.escape(rendered)}\\Z", exact=True)
        if rule not in self.allow:
            self.allow.append(rule)
        return rule


BUILTIN_DENY: tuple[PolicyRule, ...] = (
    # credential variables by name (the agent's own LYNDRIX_API_KEY stays usable)
    PolicyRule(tool="Bash", match=r"\b(CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|LYNDRIX_AI_BRIDGE_TOKEN)\b"),
    # another process's (or our own) start-up environment
    PolicyRule(tool="Bash", match=r"/proc/\S*environ"),
    # dumping the whole environment: env/printenv (with options or VAR=x only), bare
    # export/export -p/set/declare -p|-x, used as a command — at the start, after ; & | ( {
    # ` or a line break, optionally behind VAR=x or command/exec/builtin. `python -m venv
    # env`, `printenv HOME`, `env FOO=1 make`, `export FOO=1`, `set -e` stay allowed.
    PolicyRule(tool="Bash", match=(
        r"(^|[;&|(`{\n])\s*([A-Za-z_]\w*=\S*\s+)*((command|exec|builtin)\s+)?"
        r"((env|printenv)(\s+(-\S*|[A-Za-z_]\w*=\S*))*|export(\s+-p)?|set|declare\s+-[a-zA-Z]*[px])"
        r"\s*($|[;&|)`>}\n])")),
    # planting code that every session's git runs: hooks, hooksPath/fsmonitor, git … --output
    PolicyRule(tool="Bash", match=r"(?i)\.git/hooks|core\.(hookspath|fsmonitor)|\bgit\b[^;&|\n]*\s--output\b"),
    PolicyRule(tool="Read", match=r"^/proc/.*environ"),
    PolicyRule(tool="Grep", match=r"(^|\s)/proc/"),
    # git internals: hooks and config run as code in every session's git commands
    *(PolicyRule(tool=t, match=r"(^|/)\.git/") for t in sorted(FILE_EDIT_TOOLS)),
)


def fallback_verdict(permission_mode: str, tool_name: str) -> Verdict:
    """What an unmatched call gets, mirroring Claude Code's own modes.

    Claude Code already applies most of this before it consults the callback
    (acceptEdits never asks for edits, bypassPermissions never asks at all),
    so this mostly matters for ``default`` and ``plan``.
    """
    if permission_mode in ("bypassPermissions", "auto"):
        return "allow"
    if permission_mode == "acceptEdits":
        return "allow" if tool_name in FILE_EDIT_TOOLS else "ask"
    if permission_mode == "plan":
        return "deny" if tool_name in MUTATING_TOOLS else "ask"
    return "ask"
