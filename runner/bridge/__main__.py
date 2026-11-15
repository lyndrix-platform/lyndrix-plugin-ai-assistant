"""``python -m bridge`` — run the bridge under uvicorn with JSON logging.

uvicorn is started with ``log_config=None`` so it leaves logging alone and its
records flow through the JSON formatter installed here. The short graceful
shutdown matters because open SSE streams would otherwise hold the process
hostage on SIGTERM (``docker stop``).

Before anything else can run next to it, the process takes its secrets out of
the environment and becomes non-dumpable (``bridge.hardening``); the app is
built from the settings read just before, not from the (scrubbed) environment.
``proxy_headers=False``: uvicorn would otherwise trust ``X-Forwarded-For`` from
127.0.0.1 — i.e. from the agent — and the local-peer check would see a forged address.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from .config import Settings
from .hardening import make_undumpable, scrub_environ
from .logging_setup import configure

log = logging.getLogger("bridge.main")


def main() -> None:
    configure(os.environ.get("LYNDRIX_AI_LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    removed = scrub_environ(os.environ)
    undumpable = make_undumpable()
    log.info("process hardened", extra={"ctx": {"env_scrubbed": removed, "undumpable": undumpable}})
    if not undumpable:
        log.warning("could not mark the bridge non-dumpable; its memory is readable by the agent's uid")

    from .app import create_app  # imports the SDK; after the scrub so nothing reads the env early

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=True,
        timeout_graceful_shutdown=5,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
