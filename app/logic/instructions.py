"""Instruction assembly for the assistant's workspace.

Claude Code reads ``CLAUDE.md`` in the working directory. We generate it from
three layers so the assistant always knows where it is and what it may touch:

1. the Lyndrix base instructions (this file — what Lyndrix is, how plugins and
   the API fit together, house rules),
2. a generated **index** of the installed plugins (id, version, purpose and the
   URL of the plugin's full guide) — the plugin stays the single source of truth:
   the assistant fetches a guide from the running plugin when it needs it and
   caches it per version under ``.lyndrix/cache/``; nothing is copied into the
   workspace by us,
3. an operator addendum (plugin setting ``extra_instructions``).

Regenerated on every workspace sync and whenever the plugin set changes.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from core.api import get_logger

log = get_logger("Plugin:AI:Instructions")

CACHE_DIR = Path(".lyndrix") / "cache"
LEGACY_INSTRUCTIONS_DIR = Path(".lyndrix") / "instructions"   # pre-0.2 copies — removed on sync
PLUGIN_API_PREFIX = "/api/plugins"
INDEX_URL = "/api/ai/plugins"

BASE_INSTRUCTIONS = """# Lyndrix AI Assistant — workspace instructions

You are the AI assistant embedded in **Lyndrix**, a modular operations platform
(Python/FastAPI core with a React shell and plugins). You run inside a sandboxed
container next to a Lyndrix instance; this directory is your working repository.

## What Lyndrix is
- The **core** boots plugins, hosts the REST API under `/api/...`, the React shell,
  identity & permissions (roles, groups, per-plugin grants), notifications, a
  cluster layer (several instances share one database and Vault) and theming.
- **Plugins** add capabilities: each one has a stable id (`lyndrix.plugin.<name>`),
  a REST router at `/api/plugins/<id>/…`, optional React routes, settings stored
  in Vault and declared permissions.
- The platform language is **German** for operators and documentation; code,
  identifiers and commit messages are **English**. Answer in the language the
  user writes in.

## Your access to Lyndrix
`LYNDRIX_API_URL` and `LYNDRIX_API_KEY` in your environment are your own Lyndrix
account (user `ai-assistant`); call the REST API with
`Authorization: Bearer $LYNDRIX_API_KEY`. You have exactly the permissions the
operator granted that account — `GET /api/me/access` lists them. If the key is
empty, the operator has not enabled API access for you: say so instead of guessing.

## House rules
- Prefer the documented APIs over guessing internals; when something is unclear,
  say so and propose how to verify it.
- Never print or store credentials, tokens or Vault contents. Configuration values
  that look like secrets are redacted on purpose.
- Changes to files in this workspace are fine; everything outside the workspace
  is out of reach by design. Do not try to work around the sandbox.
- Be concise. Lead with the answer, then the reasoning that matters.

## Installed plugins
The list below is generated from the running instance (plugin manifests): what
each plugin is for and, when the plugin provides one, the URL of its **full
operating guide**. The plugin is the single source of truth for that guide.

**Before you operate a plugin** — call its API, change its data, explain its
behaviour in depth — read its full guide:

```bash
curl -sS -H "Authorization: Bearer $LYNDRIX_API_KEY" "$LYNDRIX_API_URL<instructions_url>" | jq -r .markdown
```

Cache it as `.lyndrix/cache/<plugin_id>@<version>.md` and reuse the cached copy
while the version in this list is unchanged; fetch again when the version differs.
The cache is ignored by git and may be deleted at any time — never copy a guide's
text anywhere else. The live index (same data as below, always current):
`GET $LYNDRIX_API_URL""" + INDEX_URL + """`.
"""


def _clean(text: str | None) -> str:
    return " ".join(str(text or "").split())


def render_plugin_index(entries: Iterable[dict]) -> str:
    """Markdown list of ``{id, name, version, status, purpose, instructions_url}``."""
    lines: list[str] = []
    for e in sorted(entries, key=lambda x: str(x.get("id", ""))):
        if not e.get("purpose") and not e.get("instructions_url"):
            continue
        head = f"- **{_clean(e.get('name')) or e.get('id')}** (`{e.get('id')}`, {e.get('version') or '?'}"
        head += f", {e.get('status')})" if e.get("status") and e.get("status") != "active" else ")"
        if e.get("purpose"):
            head += f" — {_clean(e['purpose'])}"
        lines.append(head)
        if e.get("instructions_url"):
            lines.append(f"  Guide: `{e['instructions_url']}`")
        else:
            lines.append("  Guide: _none — the purpose above is all this plugin tells you._")
    return "\n".join(lines) if lines else "_No installed plugin describes itself yet._"


def write_instructions(workspace: Path, plugin_index: Iterable[dict], extra: str | None = None) -> dict:
    """Write ``CLAUDE.md`` (base + plugin index + operator notes); returns a summary.

    Also prepares ``.lyndrix/cache/`` (git-ignored) for the guides the assistant
    fetches, and removes the pre-0.2 per-plugin copies so no stale text survives.
    """
    workspace = Path(workspace)
    entries = [dict(e) for e in plugin_index]
    (workspace / CACHE_DIR).mkdir(parents=True, exist_ok=True)
    legacy = workspace / LEGACY_INSTRUCTIONS_DIR
    if legacy.is_dir():
        shutil.rmtree(legacy, ignore_errors=True)
    _ensure_gitignore(workspace, f"{CACHE_DIR.as_posix()}/")

    parts = [BASE_INSTRUCTIONS.rstrip("\n"), "", render_plugin_index(entries)]
    if extra and extra.strip():
        parts.append("\n## Operator notes\n\n" + extra.strip())
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts.append(f"\n<!-- generated by lyndrix-plugin-ai-assistant on {stamp}; edit the plugin manifests, not this file -->\n")
    (workspace / "CLAUDE.md").write_text("\n".join(parts), encoding="utf-8")
    listed = [e["id"] for e in entries if e.get("purpose") or e.get("instructions_url")]
    log.info(f"Instructions written: {len(listed)} plugin(s) indexed, extra={'yes' if extra else 'no'}")
    return {"plugins": sorted(listed), "has_extra": bool(extra and extra.strip())}


def _ensure_gitignore(workspace: Path, line: str) -> None:
    gi = workspace / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
    if line not in existing:
        gi.write_text("\n".join([*existing, line]).strip("\n") + "\n", encoding="utf-8")


def collect_plugin_index() -> list[dict]:
    """The same index the core serves at ``GET /api/ai/plugins`` — built locally from
    the read-only registry view (core API 1.11/1.12), so the generator works on the
    runner instance without an HTTP round trip. ``remote`` plugins count: they are
    enabled cluster-wide and reachable through the load balancer."""
    try:
        from core.api import module_registry  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        log.debug(f"Instructions: no registry access ({exc})")
        return []
    status_of = getattr(module_registry, "status", None)
    out: list[dict] = []
    for m in module_registry.get_manifests():
        if getattr(m, "type", "PLUGIN") != "PLUGIN":
            continue
        purpose = (getattr(m, "ai_instructions", None) or "").strip()
        route = getattr(m, "ai_instructions_route", None)
        if not purpose and not route:
            continue
        status = status_of(str(m.id)) if callable(status_of) else ("active" if module_registry.is_active(str(m.id)) else None)
        if status not in ("active", "remote"):
            continue
        out.append({
            "id": str(m.id), "name": str(getattr(m, "name", m.id)), "version": str(getattr(m, "version", "") or ""),
            "status": status, "purpose": purpose or None,
            "instructions_url": f"{PLUGIN_API_PREFIX}/{m.id}{route}" if route else None,
        })
    return out
