"""The assistant's working repository on the shared storage.

``STORAGE_DIR/ai_workspace/repo`` is a git repository the runner container
mounts at ``/workspace``. It always exists locally (``git init`` on first use);
when the operator configures ``workspace_repo_url`` it follows that remote.
``CLAUDE.md`` is regenerated on every sync.

Trust boundary (AI-05): ``repo/`` is writable by the agent, the rest of
``ai_workspace/`` is not (only ``repo/`` is mounted into the runner). The core
therefore

* never follows a symlink inside ``repo/`` — every write, read, chown and delete
  there goes through :mod:`safe_fs`;
* never runs git with ``repo/`` as its repository. A repository's own
  ``.git/config`` can make almost any git command execute programs
  (``core.fsmonitor``, hooks, ``filter.*`` drivers, credential helpers, URL
  rewrites …) and no set of ``-c`` overrides covers all of them. Core-side git only
  ever touches directories the agent cannot write: a bare mirror of the remote
  (``origin.git``) and a staging directory for the very first checkout. Git
  commands that must run *in* the workspace (``status``, the fast-forward) are
  executed inside the runner container as the agent user (``sandbox_git``), where
  a hostile config gains nothing it does not already have.

All functions are blocking (git subprocesses) — call them via ``asyncio.to_thread``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core.api import get_logger

from . import safe_fs
from .instructions import collect_plugin_index, write_instructions

log = get_logger("Plugin:AI:Workspace")

GIT_TIMEOUT_S = 120
BUNDLE_PATH = ".lyndrix/cache/origin.bundle"      # git-ignored (see instructions.CACHE_DIR)
DIRTY_CACHE_S = 30.0

# Runs ``git <args>`` inside the runner container, in /workspace, as the agent user.
# Returns ``None`` when no runner is available on this instance.
SandboxGit = Callable[..., Optional[subprocess.CompletedProcess]]


def _with_token(url: str, token: Optional[str]) -> str:
    """``https://TOKEN@host/path`` for a private remote; untouched otherwise."""
    if not token or not url.startswith("https://") or "@" in url.split("//", 1)[1].split("/", 1)[0]:
        return url
    return url.replace("https://", f"https://{token}@", 1)


def _public_url(url: Optional[str]) -> Optional[str]:
    """The remote without credentials an operator may have pasted into it."""
    if not url:
        return None
    scheme, sep, rest = url.partition("://")
    if sep and "@" in rest.split("/", 1)[0]:
        return f"{scheme}://{rest.split('@', 1)[1]}"
    return url


def _check_remote_url(url: str) -> str:
    """Refuse values git would read as an option or as a remote-helper command."""
    url = url.strip()
    if not url or url.startswith("-") or "::" in url.split("/", 1)[0]:
        raise RuntimeError("workspace_repo_url is not a plain repository URL")
    return url


class WorkspaceManager:
    def __init__(self, storage_dir: Path, sandbox_git: Optional[SandboxGit] = None) -> None:
        self.root = Path(storage_dir) / "ai_workspace"
        self.repo = self.root / "repo"            # agent-writable (mounted at /workspace)
        self.mirror = self.root / "origin.git"    # core-only: bare mirror of the remote branch
        self.sandbox_git = sandbox_git
        self.last_sync: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.remote_url: Optional[str] = None     # configured remote, credential-free (for the status)
        self.update_pending = False               # a fetched remote update waits for the runner
        self._pending_branch: Optional[str] = None
        self._pending_tip: Optional[str] = None
        self._delivered_tip: Optional[str] = None
        self._dirty_cache: Optional[tuple[float, Optional[int]]] = None

    # ------------------------------------------------------------------ core-side git (trusted dirs only)

    def _git(self, cwd: Path, *args: str, timeout: int = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
        """Run git in a directory only the core can write. The environment is built
        from scratch: no system or user config, no prompts, no inherited ``GIT_*``."""
        cwd = Path(cwd).resolve()
        repo = self.repo.resolve()
        if cwd == repo or repo in cwd.parents:
            raise RuntimeError("refusing to run git inside the agent-writable workspace")
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.root),               # core-owned; holds no .gitconfig we rely on
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "LC_ALL": "C",
        }
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "protocol.ext.allow=never", *args],
            cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False, env=env,
        )

    @staticmethod
    def _check(res: subprocess.CompletedProcess, what: str) -> None:
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip()
            # a pasted token must never end up in logs or the UI
            raise RuntimeError(f"{what} failed: {detail.split('@')[-1][:300] if '://' in detail else detail[:300]}")

    # ------------------------------------------------------------------ state

    def is_initialized(self) -> bool:
        return safe_fs.kind(self.repo, ".git") == "dir"

    def _branch(self) -> Optional[str]:
        """Current branch from ``.git/HEAD`` — read as a file, git is not asked."""
        head = (safe_fs.read_text(self.repo, ".git/HEAD", max_bytes=4096) or "").strip()
        if head.startswith("ref: refs/heads/"):
            return head[len("ref: refs/heads/"):][:200] or None
        return "HEAD" if head else None

    def _dirty_files(self) -> Optional[int]:
        """Uncommitted entries, counted by ``git status`` inside the runner (``None`` =
        unknown: no runner on this instance). Cached briefly — the status is polled."""
        now = time.monotonic()
        if self._dirty_cache and now - self._dirty_cache[0] < DIRTY_CACHE_S:
            return self._dirty_cache[1]
        count: Optional[int] = None
        if self.sandbox_git is not None:
            try:
                res = self.sandbox_git("status", "--porcelain", timeout=20)
                if res is not None and res.returncode == 0:
                    count = len([ln for ln in res.stdout.splitlines() if ln.strip()])
            except Exception as exc:  # noqa: BLE001 — informational only
                log.debug(f"Workspace: dirty count unavailable ({exc})")
        self._dirty_cache = (now, count)
        return count

    def status(self) -> dict:
        exists = self.is_initialized()
        return {
            "path": str(self.repo),
            "initialized": exists,
            "remote": self.remote_url,
            "branch": self._branch() if exists else None,
            "dirty_files": self._dirty_files() if exists else 0,
            "has_instructions": safe_fs.kind(self.repo, "CLAUDE.md") == "file",
            "update_pending": self.update_pending,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ sync

    def sync(self, *, repo_url: Optional[str], branch: str, token: Optional[str],
             extra_instructions: Optional[str]) -> dict:
        """Make the repository exist and current, then regenerate the instructions."""
        self.last_error = None
        branch = branch or "main"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.repo.mkdir(exist_ok=True)
            self.remote_url = _public_url(repo_url.strip()) if repo_url else None
            if repo_url:
                self._sync_remote(_check_remote_url(repo_url), branch, token)
            elif safe_fs.is_empty_dir(self.repo):
                self._populate(self._stage_local(branch))
            summary = write_instructions(self.repo, collect_plugin_index(), extra_instructions)
            self.ensure_agent_access()
            self.last_sync = datetime.now(timezone.utc)
            self._dirty_cache = None
            return {"ok": True, **summary, **self.status()}
        except Exception as exc:  # noqa: BLE001 — surfaced in the status, never raised into setup()
            self.last_error = str(exc)[:500]
            log.error(f"Workspace sync failed: {exc}")
            return {"ok": False, "error": self.last_error, **self.status()}

    def _staging(self) -> Path:
        """A fresh directory next to (not inside) the workspace — core-only."""
        return Path(tempfile.mkdtemp(prefix=".staging-", dir=str(self.root)))

    def _stage_local(self, branch: str) -> Path:
        staging = self._staging()
        self._check(self._git(staging, "init", "-q", "-b", branch), "git init")
        self._identity(staging)
        (staging / "README.md").write_text(
            "# Lyndrix AI workspace\n\nWorking repository of the Lyndrix AI assistant. "
            "`CLAUDE.md` is generated by the plugin.\n",
            encoding="utf-8",
        )
        return staging

    def _identity(self, repo: Path) -> None:
        self._git(repo, "config", "user.email", "lyndrix-ai@localhost")
        self._git(repo, "config", "user.name", "Lyndrix AI Assistant")

    def _populate(self, staging: Path) -> None:
        """Move a repository prepared in ``staging`` into the (empty) workspace."""
        try:
            safe_fs.move_entries(staging, self.repo)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _sync_remote(self, repo_url: str, branch: str, token: Optional[str]) -> None:
        """Fetch the remote branch into the core-only mirror, then bring the workspace
        up to it: a first checkout is prepared in staging and moved in; later updates
        are handed over as a bundle and fast-forwarded inside the runner."""
        tip = self._fetch_mirror(repo_url, branch, token)
        if not self.is_initialized():
            if not safe_fs.is_empty_dir(self.repo):
                raise RuntimeError(f"{self.repo} exists but is not a git repository")
            staging = self._staging()
            try:
                self._check(self._git(self.root, "clone", "-q", "--branch", branch, "--single-branch",
                                      str(self.mirror), str(staging)), "git clone")
                # The checkout must name the real remote, not the core's private mirror path.
                self._check(self._git(staging, "remote", "set-url", "origin", _public_url(repo_url) or repo_url),
                            "git remote set-url")
                self._identity(staging)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            self._populate(staging)
            self._delivered_tip = tip
            self.update_pending = False
            return
        if tip == self._delivered_tip:
            return
        self._deliver_bundle(branch)
        self.update_pending, self._pending_branch, self._pending_tip = True, branch, tip
        self.apply_pending_update()

    def _fetch_mirror(self, repo_url: str, branch: str, token: Optional[str]) -> str:
        if not (self.mirror / "HEAD").is_file():
            self._check(self._git(self.root, "init", "-q", "--bare", str(self.mirror)), "git init --bare")
        ref = f"refs/heads/{branch}"
        self._check(self._git(self.mirror, "fetch", "-q", "--no-tags", "--", _with_token(repo_url, token),
                              f"+{ref}:{ref}"), "git fetch")
        res = self._git(self.mirror, "rev-parse", "--verify", ref, timeout=20)
        self._check(res, "git rev-parse")
        return res.stdout.strip()

    def _deliver_bundle(self, branch: str) -> None:
        """Hand the fetched branch to the workspace as one file (written link-free)."""
        fd, tmp = tempfile.mkstemp(prefix=".bundle-", dir=str(self.root))
        os.close(fd)
        try:
            self._check(self._git(self.mirror, "bundle", "create", tmp, f"refs/heads/{branch}"), "git bundle")
            with open(tmp, "rb") as src, safe_fs.atomic_writer(self.repo, BUNDLE_PATH, create_parents=True) as dst:
                shutil.copyfileobj(src, dst)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        self.ensure_agent_access()

    def apply_pending_update(self) -> bool:
        """Fast-forward the workspace to the delivered bundle — inside the runner, as
        the agent. Without a runner the update stays pending (``status.update_pending``)
        and is applied on the next sync or once the runner is up."""
        if not self.update_pending or self.sandbox_git is None or not self._pending_branch:
            return False
        try:
            # Only tracked changes block the fast-forward. Untracked files — the generated
            # CLAUDE.md/.gitignore, the agent's scratch files — are never touched by it; when
            # an incoming commit adds the same path, git refuses on its own ("untracked
            # working tree files would be overwritten") and that error is reported below.
            st = self.sandbox_git("status", "--porcelain", "--untracked-files=no", timeout=30)
            if st is None:
                log.info("Workspace: remote update fetched; it is merged once the runner is available.")
                return False
            if st.returncode != 0:
                raise RuntimeError((st.stderr or st.stdout or "git status failed").strip()[:300])
            if st.stdout.strip():
                log.warning("Workspace has uncommitted changes — remote update not merged (fast-forward skipped).")
                return False
            res = self.sandbox_git("pull", "-q", "--ff-only", BUNDLE_PATH, self._pending_branch, timeout=GIT_TIMEOUT_S)
            if res is None:
                return False
            if res.returncode != 0:
                raise RuntimeError((res.stderr or res.stdout or "git pull failed").strip()[:300])
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"remote update not merged: {str(exc)[:400]}"
            log.warning(f"Workspace: {self.last_error}")
            return False
        self.update_pending = False
        self._delivered_tip = self._pending_tip or self._delivered_tip
        self._dirty_cache = None
        return True

    def regenerate_instructions(self, extra_instructions: Optional[str]) -> dict:
        if not self.repo.is_dir():
            return {"ok": False, "error": "workspace not initialized"}
        summary = write_instructions(self.repo, collect_plugin_index(), extra_instructions)
        self.ensure_agent_access()
        return {"ok": True, **summary}

    def ensure_agent_access(self, uid: int = 10000, gid: int = 10000) -> None:
        """The runner works as user ``agent`` (uid/gid 10000); the core creates files as
        root. Hand the mounted tree over when we may (root), otherwise fall back to
        group/world-writable bits so the agent can still edit. Neither path follows a
        symlink: a link the agent planted is re-owned itself, its target is left alone."""
        if not self.repo.is_dir():
            return
        if os.geteuid() == 0:
            try:
                safe_fs.chown_tree(self.repo, uid, gid)
                return
            except OSError as exc:
                log.warning(f"Workspace chown to {uid}:{gid} failed ({exc}); falling back to permission bits.")
        try:
            safe_fs.open_permissions_tree(self.repo)
        except OSError as exc:
            log.warning(f"Workspace permission fallback failed — {exc}")

    def read_instructions(self) -> str:
        return safe_fs.read_text(self.repo, "CLAUDE.md") or ""
