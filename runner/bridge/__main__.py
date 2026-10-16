"""``python -m bridge`` — run the bridge under uvicorn with JSON logging.

uvicorn is started with ``log_config=None`` so it leaves logging alone and its
records flow through the JSON formatter installed here. The short graceful
shutdown matters because open SSE streams would otherwise hold the process
hostage on SIGTERM (``docker stop``).
"""

from __future__ import annotations

import os

import uvicorn

from .config import Settings
from .logging_setup import configure


def main() -> None:
    configure(os.environ.get("LYNDRIX_AI_LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    uvicorn.run(
        "bridge.app:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=True,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
