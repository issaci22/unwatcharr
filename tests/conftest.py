"""Fixtures for the fast, pure-logic tests.

`tests/e2e/` has its own conftest that boots real subprocesses; nothing here
touches the network. Tests that need a database get a throwaway one per test,
because `app.db` holds a single module-level connection.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_config(monkeypatch):
    """A fresh CONFIG_DIR and a fresh database for one test.

    `app.db` caches its connection in a module global and `app.config` reads the
    environment at import time, so both have to be repointed rather than just
    setting an env var.
    """
    from app import config, db, logging_conf

    directory = Path(tempfile.mkdtemp(prefix="unwatcharr-test-"))
    monkeypatch.setattr(config, "CONFIG_DIR", directory)
    monkeypatch.setattr(config, "DB_PATH", directory / "unwatcharr.db")
    monkeypatch.setattr(config, "LOG_DIR", directory / "logs")
    # No env pre-seeds leaking in from the developer's shell.
    monkeypatch.setattr(config, "SEED_PLEX_URL", "")
    monkeypatch.setattr(config, "SEED_PLEX_TOKEN", "")

    db.close()
    logging_conf._secrets.clear()
    try:
        db.connect()
        yield directory
    finally:
        db.close()
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def store(temp_config):
    """A bootstrapped store on a throwaway database."""
    from app import store as store_module

    store_module.ensure_bootstrap()
    return store_module


class FakeLibrary:
    """Stands in for plex.types.Library in sync_libraries()."""

    def __init__(self, section_key: str, title: str, type_: str):
        self.section_key = section_key
        self.title = title
        self.type = type_
        self.uuid = f"uuid-{section_key}"

    @property
    def supported(self) -> bool:
        return self.type in ("movie", "show")


class FakeAccount:
    """Stands in for plex.types.PlexAccount in upsert_user()."""

    def __init__(self, plex_id: str, title: str, kind: str = "home"):
        self.plex_id = plex_id
        self.title = title
        self.kind = kind
        self.uuid = None
        self.username = title.lower()
        self.email = None
        self.thumb = None
        self.protected = False


@pytest.fixture
def fake_library():
    return FakeLibrary


@pytest.fixture
def fake_account():
    return FakeAccount
