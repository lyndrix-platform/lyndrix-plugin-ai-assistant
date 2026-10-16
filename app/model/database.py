"""Database helpers for the AI Assistant plugin (cluster-safe)."""
from __future__ import annotations

import time
from contextlib import contextmanager, nullcontext
from typing import Callable, Generator, TypeVar

from sqlalchemy.orm import Session

from core.api import db_instance, get_logger

from .models import _Base

log = get_logger("Plugin:AI:DB")

T = TypeVar("T")

# A lost bootstrap race: a peer instance created the table (1050) or index (1061)
# between our inspect and our DDL — "a peer did it", safe to ignore.
_DDL_RACE_CODES = ("1050", "1061")
_DDL_RACE_TEXT = ("already exists", "duplicate key name")

# MariaDB >= 11.6 runs innodb_snapshot_isolation=ON: a REPEATABLE READ transaction
# updating a row another transaction changed fails with 1020 instead of waiting.
# Sessions here run READ COMMITTED; short writers retry on 1020 / 1205 / 1213.
_RETRY_CODES = ("1020", "1205", "1213")


def _is_ddl_race(exc: Exception) -> bool:
    msg = str(getattr(exc, "orig", exc))
    return any(code in msg for code in _DDL_RACE_CODES) or any(
        t in msg.lower() for t in _DDL_RACE_TEXT
    )


def _is_retryable(exc: Exception) -> bool:
    return any(code in str(getattr(exc, "orig", exc)) for code in _RETRY_CODES)


def _bootstrap_lock():
    """``db_instance.advisory_lock("lyndrix_bootstrap")`` on cores that have it."""
    lock = getattr(db_instance, "advisory_lock", None)
    return lock("lyndrix_bootstrap") if callable(lock) else nullcontext()


def db_ready() -> bool:
    return bool(db_instance.is_connected and db_instance.engine and db_instance.SessionLocal)


def ensure_tables() -> None:
    """Create the plugin tables (idempotent, serialized across booting instances).

    Blocking — call it via ``asyncio.to_thread``.
    """
    if not db_ready():
        raise RuntimeError("Database engine not ready — call after db:connected event")
    with _bootstrap_lock():
        try:
            _Base.metadata.create_all(db_instance.engine, checkfirst=True)
        except Exception as exc:
            if not _is_ddl_race(exc):
                raise
            log.info(f"DB: create_all lost a bootstrap race ({exc}); re-checking")
            _Base.metadata.create_all(db_instance.engine, checkfirst=True)
    log.info("DB: ai_assistant tables ready")


def retry_write(fn: Callable[[], T], *, attempts: int = 4, pause: float = 0.05) -> T:
    """Run ``fn()`` — one complete short transaction — again on 1020/1205/1213.

    ``fn`` must open its own session so every attempt starts from fresh reads.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if attempt + 1 < attempts and _is_retryable(exc):
                log.debug(f"DB: retrying write after {exc!s:.120}")
                time.sleep(pause * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a READ COMMITTED session (no-op on SQLite), rolling back on exception."""
    if not db_ready():
        raise RuntimeError("Database not connected")
    session: Session = db_instance.SessionLocal()
    try:
        _read_committed(session)
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _read_committed(session: Session) -> None:
    """Pin the session to READ COMMITTED on MySQL/MariaDB (see _RETRY_CODES)."""
    try:
        bind = session.get_bind()
        if bind is not None and bind.dialect.name in ("mysql", "mariadb"):
            session.connection(execution_options={"isolation_level": "READ COMMITTED"})
    except Exception as exc:  # isolation is an optimisation, never a reason to fail
        log.debug(f"DB: READ COMMITTED not applied: {exc}")
