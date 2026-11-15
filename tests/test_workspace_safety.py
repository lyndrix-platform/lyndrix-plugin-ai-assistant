"""AI-05: the workspace is agent-writable. The core must never follow a symlink in it
and never run git on it (ported from the audit probe ``probe_policy_symlink.py``)."""
import os
import subprocess
from pathlib import Path

import pytest

from ai_plugin.app.logic import safe_fs
from ai_plugin.app.logic import workspace as ws_mod
from ai_plugin.app.logic.instructions import write_instructions
from ai_plugin.app.logic.workspace import BUNDLE_PATH, WorkspaceManager

GIT_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@localhost", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@localhost"}


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), env={**GIT_ENV, "HOME": str(cwd)},
                          capture_output=True, text=True, check=True)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    d = tmp_path / "outside"
    d.mkdir()
    (d / "main.py").write_text("print('core')\n")
    (d / "config.py").write_text("X = 1\n")
    return d


def test_generated_files_replace_a_planted_symlink_instead_of_writing_through(tmp_path, outside):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "CLAUDE.md").symlink_to(outside / "main.py")      # the agent: ln -sf /app/main.py /workspace/CLAUDE.md
    (ws / ".gitignore").symlink_to(outside / "config.py")
    write_instructions(ws, [], None)
    assert (outside / "main.py").read_text() == "print('core')\n"
    assert (outside / "config.py").read_text() == "X = 1\n"
    assert not (ws / "CLAUDE.md").is_symlink() and "# Lyndrix AI Assistant" in (ws / "CLAUDE.md").read_text()
    assert not (ws / ".gitignore").is_symlink() and (ws / ".gitignore").read_text() == ".lyndrix/cache/\n"


def test_symlinked_directory_is_refused_but_instructions_are_still_written(tmp_path, outside):
    ws = tmp_path / "ws"
    ws.mkdir()
    (outside / "instructions").mkdir()
    (outside / "instructions" / "keep.md").write_text("keep")
    (ws / ".lyndrix").symlink_to(outside)                    # mkdir/rmtree would act in `outside`
    write_instructions(ws, [], None)
    assert not (outside / "cache").exists()
    assert (outside / "instructions" / "keep.md").read_text() == "keep"
    assert (ws / "CLAUDE.md").is_file()
    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.make_dirs(ws, ".lyndrix/cache")
    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.write_text(ws, "../escape.txt", "x")
    with pytest.raises(safe_fs.UnsafePathError):
        safe_fs.write_text(ws, "/etc/escape.txt", "x")
    assert safe_fs.read_text(ws, ".lyndrix/main.py") is None  # never read through the link either


def test_permission_fallback_and_chown_never_touch_link_targets(tmp_path, outside, monkeypatch):
    ws = tmp_path / "storage" / "ai_workspace" / "repo"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "plain.txt").write_text("x")
    secret = outside / "main.py"
    secret.chmod(0o600)
    (ws / "x").symlink_to(secret)
    (ws / "dirlink").symlink_to(outside)
    wm = WorkspaceManager(tmp_path / "storage")

    wm.ensure_agent_access()                                  # not root here → permission-bit fallback
    assert secret.stat().st_mode & 0o777 == 0o600
    assert (outside / "config.py").stat().st_mode & 0o777 != 0o666
    assert (ws / "sub" / "plain.txt").stat().st_mode & 0o777 == 0o666

    calls = []
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: calls.append(("fchown", os.readlink(f"/proc/self/fd/{fd}"))))
    monkeypatch.setattr(os, "chown", lambda name, uid, gid, **kw: calls.append((name, kw)))
    safe_fs.chown_tree(ws, 10000, 10000)
    by_name = {c[0]: c[1] for c in calls if c[0] != "fchown"}
    assert set(by_name) == {"x", "dirlink", "plain.txt"}
    assert all(kw.get("follow_symlinks") is False and "dir_fd" in kw for kw in by_name.values())
    # directories are changed by descriptor, and the walk never left the workspace
    assert all(str(ws) in path for kind, path in calls if kind == "fchown")


def test_core_never_runs_git_on_the_agent_repository(tmp_path, monkeypatch):
    """A repository's own config can make `git status` run programs (core.fsmonitor,
    filter drivers). status()/sync()/regenerate must not hand it to git at all."""
    storage = tmp_path / "storage"
    wm = WorkspaceManager(storage)
    assert wm.sync(repo_url=None, branch="main", token=None, extra_instructions=None)["ok"]
    assert wm.is_initialized() and wm.status()["branch"] == "main"
    marker = tmp_path / "pwned"
    git(wm.repo, "config", "core.fsmonitor", f"touch {marker}; false")
    git(wm.repo, "config", "filter.evil.clean", f"touch {marker}; cat")
    (wm.repo / ".gitattributes").write_text("* filter=evil\n")
    (wm.repo / "README.md").write_text("changed\n")

    seen = []
    real_run = subprocess.run

    def spy(cmd, *a, **kw):
        seen.append((list(cmd), kw.get("cwd")))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(ws_mod.subprocess, "run", spy)
    st = wm.status()
    assert wm.sync(repo_url=None, branch="main", token=None, extra_instructions="x")["ok"]
    wm.regenerate_instructions(None)
    assert not marker.exists()
    assert seen == []                                          # no git at all for an existing local repo
    assert st["initialized"] and st["dirty_files"] is None     # unknown without a runner — not guessed
    with pytest.raises(RuntimeError):
        wm._git(wm.repo / ".git", "status")                    # the guard itself


def test_remote_sync_uses_a_core_only_mirror_and_merges_in_the_sandbox(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-q", "-b", "main")
    (remote / "a.txt").write_text("one\n")
    git(remote, "add", "."), git(remote, "commit", "-q", "-m", "one")

    storage = tmp_path / "storage"
    sandbox_calls = []

    def sandbox_git(*args, timeout=60):                        # stands in for `docker exec … git` in the runner
        sandbox_calls.append(args)
        return subprocess.run(["git", *args], cwd=str(storage / "ai_workspace" / "repo"),
                              env={**GIT_ENV, "HOME": str(tmp_path)}, capture_output=True, text=True)

    wm = WorkspaceManager(storage)                             # no runner yet
    core_git_cwds = []
    real_git = wm._git
    monkeypatch.setattr(wm, "_git", lambda cwd, *a, **kw: (core_git_cwds.append(Path(cwd)), real_git(cwd, *a, **kw))[1])

    res = wm.sync(repo_url=str(remote), branch="main", token=None, extra_instructions=None)
    assert res["ok"], res
    assert (wm.repo / "a.txt").read_text() == "one\n" and res["branch"] == "main"
    assert git(wm.repo, "remote", "get-url", "origin").stdout.strip() == str(remote)   # not the mirror path

    (remote / "b.txt").write_text("two\n")
    git(remote, "add", "."), git(remote, "commit", "-q", "-m", "two")
    res = wm.sync(repo_url=str(remote), branch="main", token=None, extra_instructions=None)
    assert res["ok"] and res["update_pending"] is True         # fetched, bundle delivered, nobody to merge it
    assert (wm.repo / BUNDLE_PATH).is_file() and not (wm.repo / "b.txt").exists()

    wm.sandbox_git = sandbox_git                               # the runner came up
    assert wm.apply_pending_update() is True
    assert (wm.repo / "b.txt").read_text() == "two\n" and wm.status()["update_pending"] is False
    assert [c[0] for c in sandbox_calls] == ["status", "pull", "status"]
    # every core-side git ran outside the agent-writable repository
    assert core_git_cwds and all(wm.repo not in (c, *c.parents) for c in core_git_cwds)

    with pytest.raises(RuntimeError):
        ws_mod._check_remote_url("--upload-pack=touch /tmp/x")
    with pytest.raises(RuntimeError):
        ws_mod._check_remote_url("ext::sh -c 'id'")


def test_sandbox_git_runs_as_the_agent_with_the_container_secrets_blanked(monkeypatch):
    """`git` in the workspace runs inside the runner (``docker exec``), as uid 10000, and
    without the credentials the container env holds (docker exec inherits it)."""
    from ai_plugin.app.logic import runner as runner_mod

    calls = []
    monkeypatch.setattr(runner_mod.RunnerManager, "container_state", staticmethod(lambda: "running"))
    monkeypatch.setattr(runner_mod, "_docker", lambda *args, timeout=30: calls.append(args) or
                        subprocess.CompletedProcess(args, 0, "", ""))
    assert runner_mod.RunnerManager.sandbox_git("status", "--porcelain").returncode == 0
    args = list(calls[0])
    assert args[:3] == ["exec", "-u", "10000:10000"] and "/workspace" in args
    assert args[-4:] == [runner_mod.CONTAINER_NAME, "git", "status", "--porcelain"]
    blanked = {args[i + 1] for i, a in enumerate(args) if a == "-e"}
    for key in runner_mod.SECRET_ENV_KEYS:
        assert f"{key}=" in blanked                            # set to empty, never inherited

    monkeypatch.setattr(runner_mod.RunnerManager, "container_state", staticmethod(lambda: None))
    assert runner_mod.RunnerManager.sandbox_git("status") is None      # no runner → unknown, not guessed


def test_remote_url_is_stored_without_credentials():
    """AI-06: the workspace remote (agent-readable .git/config) never carries a token."""
    assert ws_mod._public_url("https://user:dummy-token-not-real@git.example/x.git") == "https://git.example/x.git"
    assert ws_mod._public_url("https://dummy-token-not-real@git.example/x.git") == "https://git.example/x.git"
    assert ws_mod._public_url("https://git.example/a@b.git") == "https://git.example/a@b.git"
    assert ws_mod._with_token("https://git.example/x.git", "dummy-token-not-real").startswith("https://dummy-token-not-real@")
