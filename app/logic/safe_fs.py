"""File operations inside a directory tree somebody else can write to.

The workspace (``STORAGE_DIR/ai_workspace/repo``) is mounted read-write into the
runner container, so the agent can replace any entry in it with a symlink — at
any time, also between a check and the use that follows. The core often runs as
root; a plain ``Path.write_text`` / ``os.chown`` / ``shutil.rmtree`` there turns
such a link into "overwrite / hand over / delete an arbitrary file of the core".

Everything here therefore works on directory file descriptors: every path
component is opened with ``O_NOFOLLOW | O_DIRECTORY`` relative to its parent, and
the final entry is created, replaced or changed through that descriptor without
following links. A symlink in the way raises :class:`UnsafePathError` instead of
being followed; a symlink as the final entry of a write is replaced, never
written through.

All functions are blocking — call them via ``asyncio.to_thread``.
"""
from __future__ import annotations

import os
import secrets
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator, Optional

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafePathError(OSError):
    """A path component is a symlink, escapes the root or is not what it must be."""


def _parts(rel: str | os.PathLike) -> list[str]:
    path = PurePosixPath(str(rel))
    if path.is_absolute():
        raise UnsafePathError(f"absolute path not allowed inside the workspace: {rel}")
    parts = [p for p in path.parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise UnsafePathError(f"path escapes the workspace root: {rel}")
    return parts


def _open_root(root: Path) -> int:
    try:
        return os.open(str(root), _DIR_FLAGS)
    except OSError as exc:
        raise UnsafePathError(f"workspace root {root} is not a plain directory: {exc}") from exc


def _open_dir_at(parent_fd: int, name: str, *, create: bool) -> int:
    """Open sub-directory ``name`` of ``parent_fd`` without following a symlink."""
    if create:
        try:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:   # ELOOP / ENOTDIR: a symlink or a file sits where a directory must be
        raise UnsafePathError(f"'{name}' is not a plain directory (symlink or file in the way)") from exc


@contextmanager
def _parent_fd(root: Path, rel: str | os.PathLike, *, create: bool = False) -> Iterator[tuple[int, str]]:
    """Yield ``(fd of the entry's parent directory, entry name)``, walked link-free."""
    parts = _parts(rel)
    fd = _open_root(Path(root))
    try:
        for name in parts[:-1]:
            nxt = _open_dir_at(fd, name, create=create)
            os.close(fd)
            fd = nxt
        yield fd, parts[-1]
    finally:
        os.close(fd)


def kind(root: Path, rel: str | os.PathLike) -> Optional[str]:
    """``"file"`` / ``"dir"`` / ``"symlink"`` / ``"other"`` of the entry itself, ``None`` when
    it (or a parent) does not exist. A symlinked parent counts as "does not exist"."""
    try:
        with _parent_fd(root, rel) as (fd, name):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except (FileNotFoundError, UnsafePathError):
        return None
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    return "file" if stat.S_ISREG(st.st_mode) else "other"


def make_dirs(root: Path, rel: str | os.PathLike) -> None:
    """``mkdir -p`` below ``root``; refuses to pass through (or end on) a symlink."""
    with _parent_fd(root, rel, create=True) as (fd, name):
        os.close(_open_dir_at(fd, name, create=True))


def read_text(root: Path, rel: str | os.PathLike, *, max_bytes: int = 1_048_576) -> Optional[str]:
    """Content of a regular file below ``root`` (``None`` when missing, a symlink, not a
    regular file or larger than ``max_bytes`` — never a file outside the tree)."""
    try:
        with _parent_fd(root, rel) as (fd, name):
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    except OSError:      # missing, a symlink (ELOOP) or a symlinked parent (UnsafePathError)
        return None
    with os.fdopen(file_fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return None
        return fh.read(max_bytes + 1)[:max_bytes].decode("utf-8", errors="replace")


@contextmanager
def atomic_writer(root: Path, rel: str | os.PathLike, *, mode: int = 0o644,
                  create_parents: bool = False) -> Iterator[BinaryIO]:
    """Write a file below ``root`` through a temp file in the same directory and
    ``rename`` it into place. The rename replaces whatever entry has that name — a
    planted symlink is replaced as a link, its target is never opened."""
    with _parent_fd(root, rel, create=create_parents) as (fd, name):
        tmp = f".{name}.{secrets.token_hex(6)}.tmp"
        # O_EXCL: the temp name must be new (a pre-planted link with that name fails here).
        file_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=fd)
        try:
            with os.fdopen(file_fd, "wb") as fh:
                yield fh
                fh.flush()
                os.fchmod(fh.fileno(), mode)
            os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
            raise


def write_text(root: Path, rel: str | os.PathLike, text: str, *, mode: int = 0o644,
               create_parents: bool = False) -> None:
    with atomic_writer(root, rel, mode=mode, create_parents=create_parents) as fh:
        fh.write(text.encode("utf-8"))


def remove_tree(root: Path, rel: str | os.PathLike) -> bool:
    """Delete a directory below ``root``; a symlink in the path is refused, a symlink as
    the entry itself is unlinked (the link, not its target). ``True`` when something went."""
    try:
        with _parent_fd(root, rel) as (fd, name):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(name, dir_fd=fd)      # fd-based: resistant to link swaps while it runs
            else:
                os.unlink(name, dir_fd=fd)
            return True
    except FileNotFoundError:
        return False


def is_empty_dir(root: Path) -> bool:
    fd = _open_root(Path(root))
    try:
        with os.scandir(fd) as it:
            return next(it, None) is None
    finally:
        os.close(fd)


def move_entries(src: Path, dst_root: Path) -> list[str]:
    """Move every top-level entry of ``src`` (a directory only we can write) into
    ``dst_root``. ``rename`` never follows a link at the destination; an entry that
    appeared there meanwhile makes the move fail rather than merge."""
    moved: list[str] = []
    src_fd = _open_root(Path(src))
    try:
        dst_fd = _open_root(Path(dst_root))
        try:
            for name in sorted(os.listdir(src_fd)):
                try:
                    os.stat(name, dir_fd=dst_fd, follow_symlinks=False)
                except FileNotFoundError:
                    os.rename(name, name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
                    moved.append(name)
                    continue
                raise UnsafePathError(f"'{name}' already exists in the workspace")
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)
    return moved


def chown_tree(root: Path, uid: int, gid: int) -> int:
    """Hand a tree over to ``uid:gid`` without ever following a symlink: directories
    are walked by descriptor (``os.fwalk`` verifies each one is the directory it
    listed), entries are changed with ``lchown`` semantics, so a link pointing out of
    the tree gets a new owner itself and its target is left alone. Returns the
    number of entries changed."""
    changed = 0
    for _dirpath, dirnames, filenames, dir_fd in os.fwalk(str(root), follow_symlinks=False):
        os.fchown(dir_fd, uid, gid)
        changed += 1
        # fwalk lists symlinks to directories under dirnames but does not descend into them;
        # the directories it does descend into are chowned by descriptor above.
        for name in filenames + [d for d in dirnames if _is_link_at(dir_fd, d)]:
            try:
                os.chown(name, uid, gid, dir_fd=dir_fd, follow_symlinks=False)
                changed += 1
            except OSError:
                continue
    return changed


def _is_link_at(dir_fd: int, name: str) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def open_permissions_tree(root: Path, *, dir_mode: int = 0o2777, file_mode: int = 0o666) -> int:
    """Fallback when the core may not chown: make the tree group/world-writable.
    Linux has no ``lchmod``, so every entry is opened with ``O_NOFOLLOW`` and changed
    through its descriptor; symlinks and special files are skipped. Returns the
    number of entries changed."""
    changed = 0
    for _dirpath, _dirnames, filenames, dir_fd in os.fwalk(str(root), follow_symlinks=False):
        try:
            os.fchmod(dir_fd, dir_mode)
            changed += 1
        except OSError:
            pass
        for name in filenames:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
            except OSError:      # a symlink (ELOOP), unreadable or gone — never chmod through it
                continue
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    os.fchmod(fd, file_mode)
                    changed += 1
            except OSError:
                pass
            finally:
                os.close(fd)
    return changed
