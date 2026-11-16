"""Test bootstrap.

Importing ``core.api`` cold trips a circular import inside lyndrix-core
(core.api → plugins → ui.layout → notifications → core.api). The running
platform never hits it because main.py loads core.services first; mirror that
order here before any plugin module pulls in core.api.

The plugin has no importable top-level name of its own (the core loads it from
its install directory and ``entrypoint.py`` uses relative imports), so the
suite addresses it as package ``ai_plugin``. Register that name here instead of
asking every developer for a symlink or a copy named ``ai_plugin``.
"""
import sys
import types
from pathlib import Path

# TODO(agent): fix the core.api ↔ ui.layout cycle in lyndrix-core and drop this.
import core.services  # noqa: F401

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

if "ai_plugin" not in sys.modules:
    _pkg = types.ModuleType("ai_plugin")
    _pkg.__path__ = [str(PLUGIN_ROOT)]      # a namespace-style package rooted at the repo
    _pkg.__package__ = "ai_plugin"
    sys.modules["ai_plugin"] = _pkg
