"""CLAUDE.md generation: base text + one import per plugin + operator notes."""
from pathlib import Path

from ai_plugin.app.logic.instructions import (
    INSTRUCTIONS_DIR,
    plugin_instruction_filename,
    write_instructions,
)


def test_writes_claude_md_with_plugin_imports(tmp_path: Path):
    summary = write_instructions(
        tmp_path,
        [("lyndrix.plugin.docker", "Docker Manager", "Manages Docker hosts.\n"),
         ("lyndrix.plugin.empty", "Empty", "   "),
         ("lyndrix.plugin.itsm", "ITSM", "Incidents and changes.")],
        extra="Keep answers short.",
    )
    claude_md = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "# Lyndrix AI Assistant" in claude_md
    assert "@.lyndrix/instructions/lyndrix.plugin.docker.md" in claude_md
    assert "@.lyndrix/instructions/lyndrix.plugin.itsm.md" in claude_md
    assert "lyndrix.plugin.empty" not in claude_md          # blank instructions are skipped
    assert "## Operator notes" in claude_md and "Keep answers short." in claude_md
    assert summary["plugins"] == ["lyndrix.plugin.docker.md", "lyndrix.plugin.itsm.md"] and summary["has_extra"]
    docker_file = tmp_path / INSTRUCTIONS_DIR / plugin_instruction_filename("lyndrix.plugin.docker")
    assert docker_file.read_text(encoding="utf-8").startswith("# Docker Manager (`lyndrix.plugin.docker`)")


def test_stale_plugin_files_are_removed_and_no_plugins_is_explicit(tmp_path: Path):
    write_instructions(tmp_path, [("lyndrix.plugin.a", "A", "a")], extra=None)
    assert (tmp_path / INSTRUCTIONS_DIR / "lyndrix.plugin.a.md").exists()
    write_instructions(tmp_path, [], extra=None)
    assert not (tmp_path / INSTRUCTIONS_DIR / "lyndrix.plugin.a.md").exists()
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "No plugin provides instructions yet" in text and "Operator notes" not in text


def test_filename_is_safe():
    assert plugin_instruction_filename("lyndrix.plugin.x/../y") == "lyndrix.plugin.x_.._y.md"
