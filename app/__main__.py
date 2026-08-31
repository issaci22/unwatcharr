"""Entrypoint: `python -m app`."""

from __future__ import annotations

import uvicorn

from . import config


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        log_level=config.LOG_LEVEL,
        access_log=config.LOG_LEVEL == "debug",
        # One worker on purpose: the scheduler, the run lock and the SQLite
        # connection are all process-local, so a second worker would double-run
        # every rule.
        workers=1,
    )


if __name__ == "__main__":
    main()
