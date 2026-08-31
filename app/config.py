"""Process-level configuration, read from the environment only.

Only values that must be known before the database is open live here. Everything
a user can change at runtime -- Plex connection, schedule, safe mode,
notifications, retention, UI password -- lives in the `settings` table instead,
so it survives a container recreate and can be edited from the browser.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Unwatcharr"
APP_SLUG = "unwatcharr"

VALID_LOG_LEVELS = ("debug", "info", "warning", "error")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


CONFIG_DIR = Path(_env("CONFIG_DIR", "./config")).expanduser()
DB_PATH = CONFIG_DIR / f"{APP_SLUG}.db"
LOG_DIR = CONFIG_DIR / "logs"

HOST = _env("HOST", "0.0.0.0")
try:
    PORT = int(_env("PORT", "8577"))
except ValueError:
    PORT = 8577

LOG_LEVEL = _env("LOG_LEVEL", "info").lower()
if LOG_LEVEL not in VALID_LOG_LEVELS:
    LOG_LEVEL = "info"

TZ = _env("TZ", "UTC")

# Optional pre-seeds, applied to the settings table on FIRST BOOT ONLY. After
# that the UI owns these values, so a stale compose file cannot silently clobber
# what was configured in the browser. Both are genuinely consumed in
# store.ensure_bootstrap().
SEED_PLEX_URL = _env("PLEX_URL")
SEED_PLEX_TOKEN = _env("PLEX_TOKEN")
