"""Boots the app and a mock Plex server for the end-to-end tests.

Both run as real subprocesses on real ports, so these exercise the actual HTTP
stack, template rendering, background run tasks and SQLite writes — not a
TestClient shortcut.

One app process and one mock serve the whole session for speed; the `client`
fixture resets both between tests.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(url: str, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return
        except httpx.HTTPError as exc:
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"{url} never came up: {last}")


@pytest.fixture(scope="session")
def mock_plex():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "mock_plex.py"), str(port)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_for(f"{url}/identity")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def app_server(mock_plex):
    config_dir = Path(tempfile.mkdtemp(prefix="unwatcharr-e2e-"))
    port = _free_port()

    env = dict(os.environ)
    env.update(
        {
            "CONFIG_DIR": str(config_dir),
            "PORT": str(port),
            "LOG_LEVEL": "warning",
            "PYTHONPATH": str(ROOT),
        }
    )
    # A stray seed from the developer's shell would change what setup does.
    for key in ("PLEX_URL", "PLEX_TOKEN"):
        env.pop(key, None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "app"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_for(f"{url}/healthz")
        yield {"url": url, "db": config_dir / "unwatcharr.db", "plex": mock_plex,
               "config_dir": config_dir}
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(config_dir, ignore_errors=True)


@pytest.fixture
def client(app_server):
    """A connected app with a clean slate.

    Wipes the tables a previous test may have filled, resets the mock's
    per-token watch state, re-runs setup, and turns safe mode OFF — the one test
    that cares about safe mode turns it back on explicitly.
    """
    httpx.post(f"{app_server['plex']}/__reset", timeout=10.0)

    conn = sqlite3.connect(app_server["db"])
    try:
        for table in ("actions", "run_passes", "runs", "rule_users",
                      "rule_libraries", "rules", "plex_users"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()

    c = httpx.Client(base_url=app_server["url"], timeout=90.0, follow_redirects=False)
    c.post("/api/setup/manual", json={"url": app_server["plex"], "token": "tok-owner"})
    c.post("/api/settings", json={"request_delay_ms": 0})
    c.post("/api/settings/safe-mode", json={"enabled": False, "confirm": True})
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def db(app_server):
    def query(sql: str, params: tuple = ()) -> list[dict]:
        conn = sqlite3.connect(app_server["db"])
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    return query


@pytest.fixture
def plex_calls(app_server):
    def calls() -> list[dict]:
        return httpx.get(f"{app_server['plex']}/__calls", timeout=10.0).json()

    return calls


@pytest.fixture
def plex_state(app_server):
    def state() -> dict:
        return httpx.get(f"{app_server['plex']}/__state", timeout=10.0).json()

    return state


@pytest.fixture
def wait_for_run(app_server):
    """Runs are background tasks, so tests must wait for one to finish."""

    def wait(c: httpx.Client, timeout: float = 60.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = c.get("/api/runs/current").json()
            if not state.get("busy"):
                return c.get("/api/runs?limit=1").json()["runs"][0]
            time.sleep(0.15)
        raise AssertionError("run did not finish in time")

    return wait
