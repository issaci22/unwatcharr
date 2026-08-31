"""Logging setup, an in-memory ring buffer for the UI log panel, and redaction.

The log is the only place a user can see what a run actually did, so the runner
narrates deliberately (see engine/runner.py). Two things this module guarantees:

1. A Plex token never reaches the log, the UI log panel, or a log file. Tokens
   turn up in URLs, in httpx debug output and in exception text from third-party
   libraries, so redaction happens at the handler rather than at every call site.
2. httpx and APScheduler are pinned to WARNING. httpx logs every request at
   INFO, which buries the run narrative on a large library.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import Any

from . import config

MAX_RECORDS = 2000
_records: deque[dict[str, Any]] = deque(maxlen=MAX_RECORDS)

# Values registered here are redacted verbatim wherever they appear. store.py
# registers every token it reads or writes.
_secrets: set[str] = set()

# Catches tokens that were never stored -- a token pasted into a form that then
# failed validation, or one echoed back inside a third-party error string.
_PATTERNS = (
    re.compile(r"(X-Plex-Token[=:]\s*)([^\s&'\"]+)", re.IGNORECASE),
    re.compile(r"([?&](?:token|authToken|authenticationToken)=)([^\s&'\"]+)", re.IGNORECASE),
    re.compile(r"('(?:token|authToken|authenticationToken)':\s*')([^']+)(')", re.IGNORECASE),
)

REDACTED = "***"


def register_secret(value: str | None) -> None:
    """Remember a token so it is scrubbed from anything logged later."""
    if value and len(value) >= 8:
        _secrets.add(str(value))


def forget_secret(value: str | None) -> None:
    _secrets.discard(str(value or ""))


def redact(text: str) -> str:
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(rf"\g<1>{REDACTED}\g<3>", text)
        else:
            text = pattern.sub(rf"\g<1>{REDACTED}", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class RingBufferHandler(logging.Handler):
    """Backs the UI log panel. Never allowed to raise -- logging must not be a
    source of failures in an app whose whole job is not breaking things."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _records.append(
                {
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": self.format(record),
                }
            )
        except Exception:
            pass


def recent(
    limit: int = 200, min_level: str = "DEBUG", search: str = ""
) -> list[dict[str, Any]]:
    """Newest first, optionally filtered by level and substring."""
    threshold = logging.getLevelName(min_level.upper())
    if not isinstance(threshold, int):
        threshold = logging.DEBUG

    needle = search.strip().lower()
    out = []
    for record in _records:
        level = logging.getLevelName(record["level"])
        if not isinstance(level, int) or level < threshold:
            continue
        if needle and needle not in record["message"].lower():
            continue
        out.append(record)
    return out[-limit:][::-1]


def clear() -> None:
    _records.clear()


def configure(to_file: bool = False) -> None:
    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    logging.Formatter.converter = time.localtime

    console_fmt = RedactingFormatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler()
    stream.setFormatter(console_fmt)
    root.addHandler(stream)

    ring = RingBufferHandler()
    ring.setFormatter(RedactingFormatter("%(message)s"))
    root.addHandler(ring)

    if to_file:
        try:
            config.LOG_DIR.mkdir(parents=True, exist_ok=True)
            rotating = RotatingFileHandler(
                config.LOG_DIR / f"{config.APP_SLUG}.log",
                maxBytes=2_000_000,
                backupCount=3,
                encoding="utf-8",
            )
            rotating.setFormatter(console_fmt)
            root.addHandler(rotating)
        except OSError as exc:
            root.warning("Could not open the log file (%s); logging to stdout only.", exc)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
