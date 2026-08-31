"""Importing a Plex-Unwatcher v1 installation.

Built against a synthetic v1 database rather than the real one, so the suite
runs anywhere. The shape below is v1's `db._SCHEMA` verbatim.

The rule these tests exist to enforce: the v1 file is READ-ONLY. An upgrade must
be able to fail and leave the old installation exactly as it was.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import store as store_module
from app.services import migrate_v1

V1_SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE plex_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, plex_id TEXT NOT NULL UNIQUE, uuid TEXT,
    title TEXT NOT NULL, username TEXT, email TEXT, thumb TEXT,
    kind TEXT NOT NULL DEFAULT 'home', token TEXT,
    token_status TEXT NOT NULL DEFAULT 'missing', token_checked_at INTEGER,
    protected INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE libraries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, section_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL, type TEXT NOT NULL, uuid TEXT, updated_at INTEGER NOT NULL);
CREATE TABLE rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, library_id INTEGER NOT NULL,
    age_value INTEGER NOT NULL DEFAULT 90, age_unit TEXT NOT NULL DEFAULT 'days',
    min_view_count INTEGER NOT NULL DEFAULT 1,
    require_series_complete INTEGER NOT NULL DEFAULT 1,
    skip_in_progress INTEGER NOT NULL DEFAULT 1,
    skip_now_playing INTEGER NOT NULL DEFAULT 1,
    clear_progress INTEGER NOT NULL DEFAULT 0,
    include_filters TEXT NOT NULL DEFAULT '[]',
    exclude_filters TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE rule_users (
    rule_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, age_value INTEGER, age_unit TEXT,
    PRIMARY KEY (rule_id, user_id));
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT, rule_id INTEGER,
    rule_name TEXT, user_id INTEGER, user_title TEXT, mode TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'manual', status TEXT NOT NULL DEFAULT 'running',
    scanned INTEGER NOT NULL DEFAULT 0, matched INTEGER NOT NULL DEFAULT 0,
    applied INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0, error TEXT,
    started_at INTEGER NOT NULL, finished_at INTEGER);
CREATE TABLE actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, user_id INTEGER,
    user_title TEXT, rating_key TEXT NOT NULL, item_type TEXT NOT NULL,
    title TEXT NOT NULL, grandparent_title TEXT, season INTEGER, episode INTEGER,
    thumb TEXT, year INTEGER, last_viewed_at INTEGER, view_count_before INTEGER,
    status TEXT NOT NULL DEFAULT 'candidate', error TEXT, applied_at INTEGER,
    undone_at INTEGER);
"""

V1_SETTINGS = {
    "plex_url": "http://10.0.0.5:32400",
    "plex_machine_id": "machine-abc",
    "plex_server_name": "BlockBuster 2.0",
    "client_identifier": "client-xyz",
    "plex_account_token": "v1-account-token",
    "setup_complete": True,
    "safe_mode": True,
    "schedule_hours": 12,
    "request_delay_ms": 250,
    "notify_url": "https://hooks.example/abc",
    "session_secret": "v1-session-secret-must-not-travel",
    "a_setting_that_no_longer_exists": "junk",
}


@pytest.fixture
def v1_db(tmp_path) -> Path:
    path = tmp_path / "plex-unwatcher.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V1_SCHEMA)
        conn.execute("PRAGMA user_version=1")
        for key, value in V1_SETTINGS.items():
            conn.execute("INSERT INTO settings (key, value) VALUES (?,?)",
                         (key, json.dumps(value)))
        conn.execute(
            "INSERT INTO libraries (id, section_key, title, type, uuid, updated_at) "
            "VALUES (1,'1','Movies','movie','u1',1000), "
            "       (2,'2','TV Shows','show','u2',1000)")
        conn.execute(
            "INSERT INTO plex_users (id, plex_id, title, username, kind, token, "
            "token_status, protected, enabled, created_at, updated_at) VALUES "
            "(1,'tv:100','Alice','alice','owner','tok-alice','ok',0,1,1000,1000),"
            "(2,'tv:200','Bob','bob','home','tok-bob','ok',0,1,1000,1000)")
        conn.execute(
            "INSERT INTO rules (id, name, enabled, library_id, age_value, age_unit, "
            "min_view_count, require_series_complete, skip_in_progress, "
            "skip_now_playing, clear_progress, include_filters, exclude_filters, "
            "created_at, updated_at) VALUES "
            "(1,'Movies cleanup',1,1,30,'days',1,0,1,1,0,'[]',"
            "'[{\"field\":\"label\",\"value\":\"keep\"}]',1000,1000),"
            "(2,'TV cleanup',1,2,90,'days',2,1,1,1,1,'[]','[]',1000,1000)")
        conn.execute(
            "INSERT INTO rule_users (rule_id, user_id, enabled, age_value, age_unit) "
            "VALUES (1,2,0,NULL,NULL), (2,2,1,7,'days')")
        # Two v1 runs sharing one batch, plus a lone run with no batch.
        conn.execute(
            "INSERT INTO runs (id, batch, rule_id, rule_name, user_id, user_title, "
            "mode, trigger, status, scanned, matched, applied, failed, skipped, "
            "started_at, finished_at) VALUES "
            "(1,'batch1',1,'Movies cleanup',1,'Alice','apply','manual','ok',100,5,5,0,95,1000,1060),"
            "(2,'batch1',2,'TV cleanup',1,'Alice','apply','manual','ok',50,2,2,0,48,1000,1080),"
            "(3,NULL,1,'Movies cleanup',2,'Bob','dry','schedule','ok',10,0,0,0,10,2000,2010)")
        conn.execute(
            "INSERT INTO actions (id, run_id, user_id, user_title, rating_key, "
            "item_type, title, year, last_viewed_at, view_count_before, status, "
            "applied_at) VALUES "
            "(1,1,1,'Alice','501','movie','Arrival',2016,900,1,'applied',1050),"
            "(2,1,1,'Alice','502','movie','Dune',2021,900,1,'applied',1051)")
        conn.commit()
    finally:
        conn.close()
    return path


def fingerprint(path: Path) -> tuple[int, int, bytes]:
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns, path.read_bytes())


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_inspect_summarises_without_importing(store, v1_db):
    summary = migrate_v1.inspect(v1_db)
    assert summary["server_name"] == "BlockBuster 2.0"
    assert summary["schema_version"] == 1
    assert summary["has_token"] is True
    assert summary["counts"] == {
        "rules": 2, "users": 2, "libraries": 2, "runs": 3, "actions": 2,
        "overrides": 2,
    }
    assert store.list_rules() == []


def test_detect_finds_a_database_in_the_config_dir(store, temp_config, v1_db):
    import shutil

    shutil.copy2(v1_db, temp_config / "plex-unwatcher.db")
    found = migrate_v1.detect()
    assert found["found"] is True
    assert found["server_name"] == "BlockBuster 2.0"


def test_detect_reports_nothing_when_there_is_nothing(store, temp_config):
    found = migrate_v1.detect()
    assert found["found"] is False
    assert found["searched"]


def test_a_random_sqlite_file_is_not_mistaken_for_v1(store, tmp_path):
    path = tmp_path / "something.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated (a)")
    conn.commit()
    conn.close()
    with pytest.raises(migrate_v1.MigrationError, match="does not look like"):
        migrate_v1.inspect(path)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def test_import_never_touches_the_source_file(store, v1_db):
    before = fingerprint(v1_db)
    migrate_v1.import_v1(v1_db)
    assert fingerprint(v1_db) == before


def test_import_brings_settings_across(store, v1_db):
    migrate_v1.import_v1(v1_db)
    assert store.get_config("plex_server_name") == "BlockBuster 2.0"
    assert store.get_config("plex_url") == "http://10.0.0.5:32400"
    assert store.get_config("schedule_hours") == 12
    assert store.get_config("request_delay_ms") == 250
    assert store.get_config("plex_account_token") == "v1-account-token"


def test_session_secret_is_never_imported(store, v1_db):
    """Session secrets must not be shared between installations."""
    original = store.get_config("session_secret")
    migrate_v1.import_v1(v1_db)
    current = store.get_config("session_secret")
    assert current == original
    assert current != V1_SETTINGS["session_secret"]


def test_unknown_v1_settings_are_ignored(store, v1_db):
    migrate_v1.import_v1(v1_db)
    assert "a_setting_that_no_longer_exists" not in store.all_config()


def test_rules_gain_a_media_type_and_a_library_join(store, v1_db):
    migrate_v1.import_v1(v1_db)
    rules = {r["name"]: r for r in store.list_rules()}

    movies = rules["Movies cleanup"]
    assert movies["media_type"] == "movie"
    assert [l["title"] for l in movies["libraries"]] == ["Movies"]
    assert movies["require_series_complete"] == 0
    assert movies["tv_scope"] == "episodes", "v1 only ever worked episode by episode"
    assert json.loads(movies["exclude_filters"]) == [{"field": "label", "value": "keep"}]

    tv = rules["TV cleanup"]
    assert tv["media_type"] == "show"
    assert [l["title"] for l in tv["libraries"]] == ["TV Shows"]
    assert tv["require_series_complete"] == 1
    assert tv["min_view_count"] == 2


def test_users_and_their_tokens_come_across(store, v1_db):
    migrate_v1.import_v1(v1_db)
    users = {u["title"]: u for u in store.list_users()}
    assert users["Alice"]["kind"] == "owner"
    assert users["Alice"]["token"] == "tok-alice"
    assert users["Bob"]["kind"] == "home"


def test_per_user_overrides_survive(store, v1_db):
    migrate_v1.import_v1(v1_db)
    rules = {r["name"]: r for r in store.list_rules()}
    users = {u["title"]: u for u in store.list_users()}

    movie_overrides = store.rule_overrides(rules["Movies cleanup"]["id"])
    assert movie_overrides[users["Bob"]["id"]]["enabled"] == 0

    tv_overrides = store.rule_overrides(rules["TV cleanup"]["id"])
    assert tv_overrides[users["Bob"]["id"]]["age_value"] == 7


def test_v1_runs_are_regrouped_by_batch(store, v1_db):
    """v1 had one row per rule x user with a shared batch column; v2 has a
    batch-level run plus a pass each."""
    migrate_v1.import_v1(v1_db)
    runs = store.recent_runs()
    assert len(runs) == 2, "batch1 collapses to one run, the batchless run stands alone"

    batched = next(r for r in runs if r["uid"] == "batch1")
    assert batched["scanned"] == 150
    assert batched["matched"] == 7
    assert batched["applied"] == 7
    assert batched["rules_processed"] == 2
    assert batched["users_processed"] == 1
    assert batched["started_at"] == 1000 and batched["finished_at"] == 1080

    passes = store.run_passes(batched["id"])
    assert sorted(p["rule_name"] for p in passes) == ["Movies cleanup", "TV cleanup"]


def test_actions_are_remapped_onto_the_new_run_and_pass(store, v1_db):
    migrate_v1.import_v1(v1_db)
    batched = next(r for r in store.recent_runs() if r["uid"] == "batch1")
    actions = store.run_actions(batched["id"])
    assert len(actions) == 2
    assert {a["title"] for a in actions} == {"Arrival", "Dune"}

    pass_ids = {p["id"] for p in store.run_passes(batched["id"])}
    assert all(a["pass_id"] in pass_ids for a in actions)
    assert all(a["rule_name"] for a in actions), "actions gain the rule that caused them"

    history, total = store.history()
    assert total == 2


def test_import_records_its_provenance(store, v1_db):
    migrate_v1.import_v1(v1_db)
    assert store.get_config("migrated_from_v1") is True
    assert store.get_config("migrated_from_v1_at") > 0


def test_importing_into_a_populated_database_is_refused(store, v1_db):
    migrate_v1.import_v1(v1_db)
    with pytest.raises(migrate_v1.MigrationError, match="already has rules or users"):
        migrate_v1.import_v1(v1_db)


def test_force_replaces_rather_than_duplicating(store, v1_db):
    """force means replace. Without clearing first, run uids carried over from
    v1 batch ids collide and every rule would be imported twice."""
    migrate_v1.import_v1(v1_db)
    result = migrate_v1.import_v1(v1_db, force=True)
    assert result["imported"]["rules"] == 2
    assert len(store.list_rules()) == 2, "a forced re-import must not duplicate"
    assert len(store.list_users()) == 2
    assert len(store.recent_runs()) == 2


def test_a_rule_pointing_at_a_missing_library_still_imports(store, v1_db):
    """It arrives with no libraries so it can be fixed by hand, rather than
    being silently dropped."""
    conn = sqlite3.connect(v1_db)
    conn.execute("DELETE FROM libraries WHERE id = 1")
    conn.commit()
    conn.close()

    migrate_v1.import_v1(v1_db)
    rules = {r["name"]: r for r in store.list_rules()}
    assert rules["Movies cleanup"]["libraries"] == []
    assert rules["TV cleanup"]["libraries"], "the intact rule is unaffected"
