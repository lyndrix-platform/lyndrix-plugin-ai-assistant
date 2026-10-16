"""CLAUDE.md generation: base text + generated plugin index + operator notes; no copied guides."""
from pathlib import Path

from ai_plugin.app.logic.instructions import CACHE_DIR, LEGACY_INSTRUCTIONS_DIR, render_plugin_index, write_instructions

INDEX = [
    {"id": "lyndrix.plugin.itsm", "name": "ITSM", "version": "0.2.0", "status": "active",
     "purpose": "Incidents and changes.", "instructions_url": "/api/plugins/lyndrix.plugin.itsm/ai/instructions"},
    {"id": "lyndrix.plugin.docker", "name": "Docker Manager", "version": "1.2.0", "status": "active",
     "purpose": "Manages Docker hosts.\n", "instructions_url": None},
    {"id": "lyndrix.plugin.ai_assistant", "name": "KI-Assistent", "version": "0.2.0", "status": "remote",
     "purpose": None, "instructions_url": "/api/plugins/lyndrix.plugin.ai_assistant/ai/instructions"},
    {"id": "lyndrix.plugin.silent", "name": "Silent", "version": "1.0.0", "status": "active", "purpose": None, "instructions_url": None},
]


def test_writes_claude_md_with_index_and_fetch_rule(tmp_path: Path):
    summary = write_instructions(tmp_path, INDEX, extra="Keep answers short.")
    md = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "# Lyndrix AI Assistant" in md and "## Installed plugins" in md
    assert "GET $LYNDRIX_API_URL/api/ai/plugins" in md and ".lyndrix/cache/<plugin_id>@<version>.md" in md
    # sorted by id, purpose inline, guide url only when the plugin serves one
    assert md.index("lyndrix.plugin.ai_assistant") < md.index("lyndrix.plugin.docker") < md.index("lyndrix.plugin.itsm")
    assert "**ITSM** (`lyndrix.plugin.itsm`, 0.2.0) — Incidents and changes." in md
    assert "Guide: `/api/plugins/lyndrix.plugin.itsm/ai/instructions`" in md
    assert "**Docker Manager** (`lyndrix.plugin.docker`, 1.2.0) — Manages Docker hosts." in md
    assert "Guide: _none" in md
    assert "(`lyndrix.plugin.ai_assistant`, 0.2.0, remote)" in md
    assert "lyndrix.plugin.silent" not in md                       # nothing to say → not listed
    assert "## Operator notes" in md and "Keep answers short." in md
    assert summary["plugins"] == ["lyndrix.plugin.ai_assistant", "lyndrix.plugin.docker", "lyndrix.plugin.itsm"]
    # cache dir exists and is git-ignored; no guide text was copied into the workspace
    assert (tmp_path / CACHE_DIR).is_dir()
    assert ".lyndrix/cache/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert not list((tmp_path / ".lyndrix").glob("instructions/*"))


def test_legacy_copies_are_removed_and_empty_index_is_explicit(tmp_path: Path):
    legacy = tmp_path / LEGACY_INSTRUCTIONS_DIR
    legacy.mkdir(parents=True)
    (legacy / "lyndrix.plugin.a.md").write_text("old copy", encoding="utf-8")
    write_instructions(tmp_path, [], extra=None)
    assert not legacy.exists()
    md = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "No installed plugin describes itself yet" in md and "Operator notes" not in md


def test_render_index_is_single_line_per_purpose():
    out = render_plugin_index([{"id": "p", "name": "P", "version": "1", "status": "active",
                                "purpose": "line one\n\nline   two", "instructions_url": None}])
    assert "— line one line two" in out


def test_manifest_version_matches_service_constant():
    import re

    from ai_plugin.app.logic.service import PLUGIN_VERSION

    text = Path(__file__).resolve().parents[1].joinpath("entrypoint.py").read_text(encoding="utf-8")
    assert re.search(r'version="([^"]+)"', text).group(1) == PLUGIN_VERSION
