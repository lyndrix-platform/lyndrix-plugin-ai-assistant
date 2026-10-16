"""Prepare the agent's HOME before Claude Code runs (called by entrypoint.sh).

Claude Code keeps global state in ``~/.claude.json`` (or
``$CLAUDE_CONFIG_DIR/.claude.json`` when that variable is set). A fresh HOME
would make the CLI treat the first run as onboarding and the workspace as
untrusted; headless runs do not show those dialogs, but the well-known
community workaround — mark onboarding done and the project trusted up front —
is cheap insurance. Keys verified against a real 2.1.301 config:
``hasCompletedOnboarding`` (top level) and ``projects.<path>.{hasTrustDialogAccepted,
allowedTools}``. Existing content is merged, never clobbered.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .config import Settings, detect_auth
from .logging_setup import configure

log = logging.getLogger("bridge.prepare_home")


def merge_claude_json(path: Path, workspace: str) -> None:
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError:
            log.warning("unreadable claude.json, rewriting", extra={"ctx": {"path": str(path)}})
            data = {}
    data["hasCompletedOnboarding"] = True
    projects = data.setdefault("projects", {})
    project = projects.setdefault(workspace, {})
    project["hasTrustDialogAccepted"] = True
    project.setdefault("allowedTools", [])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def main() -> None:
    configure(os.environ.get("LYNDRIX_AI_LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    home = Path(os.environ.get("HOME", "/home/agent"))
    targets = [home / ".claude.json"]
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        candidate = Path(config_dir) / ".claude.json"
        if candidate not in targets:
            targets.append(candidate)
    else:
        (home / ".claude").mkdir(parents=True, exist_ok=True)
    for target in targets:
        merge_claude_json(target, settings.workspace)
    kind, warnings = detect_auth()
    log.info(
        "home prepared",
        extra={"ctx": {"claude_json": [str(t) for t in targets], "workspace": settings.workspace, "auth": kind}},
    )
    for warning in warnings:
        log.warning(warning)


if __name__ == "__main__":
    main()
