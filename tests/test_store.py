"""The repository layer: schema, override resolution, and secret handling."""

from __future__ import annotations

import pytest

from app import db, logging_conf, migrations


# ---------------------------------------------------------------------------
# Schema and migrations
# ---------------------------------------------------------------------------

def test_migrations_build_the_schema_from_zero(temp_config):
    """Fresh install and upgrade take the same code path, so the migration
    machinery is exercised on every install rather than only in theory."""
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "settings", "plex_users", "libraries", "rules", "rule_libraries",
        "rule_users", "runs", "run_passes", "actions",
    } <= tables


def test_applying_migrations_twice_is_a_no_op(temp_config):
    conn = db.connect()
    assert migrations.apply(conn) == migrations.SCHEMA_VERSION
    assert migrations.apply(conn) == migrations.SCHEMA_VERSION


def test_a_newer_database_is_left_alone(temp_config, caplog):
    conn = db.connect()
    conn.execute(f"PRAGMA user_version={migrations.SCHEMA_VERSION + 5}")
    conn.commit()
    assert migrations.apply(conn) == migrations.SCHEMA_VERSION + 5


def test_read_only_open_refuses_writes(temp_config):
    import sqlite3

    db.connect()
    db.close()
    conn = db.open_readonly(temp_config / "unwatcharr.db")
    try:
        conn.execute("SELECT COUNT(*) FROM settings").fetchone()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO settings (key, value) VALUES ('x','1')")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Config and secrets
# ---------------------------------------------------------------------------

def test_safe_mode_defaults_on(store):
    assert store.get_config("safe_mode") is True


def test_bootstrap_generates_an_identity_and_a_session_secret(store):
    assert store.get_config("client_identifier")
    assert store.get_config("session_secret")


def test_public_config_strips_every_secret(store):
    store.set_config("plex_account_token", "tok-secret-value")
    store.set_config("ui_password_hash", "hashed")
    public = store.public_config()
    for key in store.SECRET_CONFIG_KEYS:
        assert key not in public
    assert public["password_set"] is True


def test_tokens_are_registered_for_log_redaction(store, fake_account):
    store.set_config("plex_account_token", "account-token-abcdef")
    store.upsert_user(fake_account("tv:1", "Alice", "owner"), token="user-token-123456")

    scrubbed = logging_conf.redact(
        "using account-token-abcdef and user-token-123456 now"
    )
    assert "account-token-abcdef" not in scrubbed
    assert "user-token-123456" not in scrubbed


def test_redaction_catches_unregistered_tokens_in_urls():
    scrubbed = logging_conf.redact("GET /library?X-Plex-Token=neverseenbefore123")
    assert "neverseenbefore123" not in scrubbed


# ---------------------------------------------------------------------------
# Libraries and rules
# ---------------------------------------------------------------------------

def test_sync_libraries_drops_unsupported_types(store, fake_library):
    count = store.sync_libraries([
        fake_library("1", "Movies", "movie"),
        fake_library("2", "TV Shows", "show"),
        fake_library("3", "Music", "artist"),
        fake_library("4", "Photos", "photo"),
    ])
    assert count == 2
    assert sorted(l["title"] for l in store.list_libraries()) == ["Movies", "TV Shows"]


def test_a_vanished_library_cascades_to_rule_membership(store, fake_library):
    store.sync_libraries([fake_library("1", "Movies", "movie"),
                          fake_library("2", "Kids", "movie")])
    ids = [l["id"] for l in store.list_libraries()]
    rule_id = store.create_rule(name="Both", media_type="movie", library_ids=ids)
    assert len(store.get_rule(rule_id)["libraries"]) == 2

    store.sync_libraries([fake_library("1", "Movies", "movie")])
    assert [l["title"] for l in store.get_rule(rule_id)["libraries"]] == ["Movies"]


def test_list_libraries_can_filter_by_media_type(store, fake_library):
    store.sync_libraries([fake_library("1", "Movies", "movie"),
                          fake_library("2", "TV", "show")])
    assert [l["title"] for l in store.list_libraries("show")] == ["TV"]


# ---------------------------------------------------------------------------
# build_rule -- the single place an override is resolved
# ---------------------------------------------------------------------------

def _rule_row(store, fake_library, **over):
    store.sync_libraries([fake_library("1", "Movies", "movie")])
    library_id = store.list_libraries()[0]["id"]
    fields = {"name": "R", "media_type": "movie", "age_value": 90,
              "age_unit": "days", "library_ids": [library_id]}
    fields.update(over)
    return store.get_rule(store.create_rule(**fields))


def test_build_rule_without_an_override_uses_the_default(store, fake_library):
    row = _rule_row(store, fake_library)
    built = store.build_rule(row, None)
    assert (built.age_value, built.age_unit) == (90, "days")


def test_build_rule_applies_a_user_override(store, fake_library):
    row = _rule_row(store, fake_library)
    built = store.build_rule(row, {"enabled": 1, "age_value": 7, "age_unit": "days"})
    assert (built.age_value, built.age_unit) == (7, "days")


def test_a_null_override_value_inherits_the_default(store, fake_library):
    """Stored as NULL rather than a copy, so changing the rule default later
    still moves everyone who never set their own."""
    row = _rule_row(store, fake_library, age_value=30)
    built = store.build_rule(row, {"enabled": 1, "age_value": None, "age_unit": None})
    assert built.age_value == 30


def test_build_rule_carries_filters_and_scope(store, fake_library):
    row = _rule_row(
        store, fake_library,
        include_filters='[{"field":"genre","value":"Sci-Fi"}]',
        exclude_filters='[{"field":"label","value":"keep"}]',
    )
    built = store.build_rule(row, None)
    assert [f.value for f in built.include_filters] == ["Sci-Fi"]
    assert [f.value for f in built.exclude_filters] == ["keep"]


def test_malformed_filter_json_degrades_to_empty(store, fake_library):
    row = _rule_row(store, fake_library, include_filters="not json at all")
    assert store.build_rule(row, None).include_filters == []


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def test_runnable_users_excludes_the_unusable(store, fake_account):
    ok = store.upsert_user(fake_account("tv:1", "Alice", "owner"), token="tok-a")
    store.upsert_user(fake_account("tv:2", "NoToken", "home"))
    disabled = store.upsert_user(fake_account("tv:3", "Disabled", "home"), token="tok-c")
    invalid = store.upsert_user(fake_account("tv:4", "Expired", "home"), token="tok-d")
    store.set_user_enabled(disabled, False)
    store.set_user_token_status(invalid, "invalid")

    assert [u["id"] for u in store.runnable_users()] == [ok]


def test_upsert_preserves_an_existing_token(store, fake_account):
    user_id = store.upsert_user(fake_account("tv:1", "Alice", "owner"), token="tok-a")
    store.upsert_user(fake_account("tv:1", "Alice Renamed", "owner"))
    user = store.get_user(user_id)
    assert user["token"] == "tok-a"
    assert user["title"] == "Alice Renamed"


def test_single_user_detection(store, fake_account):
    assert store.is_single_user()
    store.upsert_user(fake_account("tv:1", "Alice", "owner"))
    assert store.is_single_user()
    store.upsert_user(fake_account("tv:2", "Bob", "home"))
    assert not store.is_single_user()


# ---------------------------------------------------------------------------
# Runs and history
# ---------------------------------------------------------------------------

def test_history_excludes_dry_run_candidates(store, fake_account, fake_library):
    from app.plex.types import MediaItem

    user = store.get_user(store.upsert_user(fake_account("tv:1", "Alice", "owner"), token="t"))
    run_id, _ = store.create_run(mode="dry", trigger="manual")
    pass_id = store.create_pass(run_id=run_id, rule_id=None, rule_name="R",
                                user_id=user["id"], user_title="Alice")
    item = MediaItem(rating_key="1", type="movie", title="A Movie", view_count=1)

    candidate = store.add_action(run_id=run_id, pass_id=pass_id, rule_id=None,
                                 rule_name="R", user=user, item=item, status="candidate")
    applied = store.add_action(run_id=run_id, pass_id=pass_id, rule_id=None,
                               rule_name="R", user=user, item=item, status="candidate")
    store.mark_action(applied, "applied")

    actions, total = store.history()
    assert total == 1
    assert actions[0]["id"] == applied


def test_set_run_mode_records_a_safe_mode_downgrade(store):
    run_id, _ = store.create_run(mode="apply", trigger="manual")
    store.set_run_mode(run_id, "dry")
    run = store.get_run(run_id)
    assert run["mode"] == "dry"
    assert run["finished_at"] is None, "downgrading must not finish the run"


def test_prune_removes_old_dry_runs_but_keeps_recent_history(store):
    from app.timeutil import now

    old_dry, _ = store.create_run(mode="dry", trigger="schedule")
    fresh_apply, _ = store.create_run(mode="apply", trigger="manual")
    store.finish_run(old_dry, status="ok")
    store.finish_run(fresh_apply, status="ok")
    db.execute("UPDATE runs SET finished_at = ? WHERE id = ?",
               (now() - 400 * 86400, old_dry))

    removed = store.prune_history(keep_days=365, dry_keep_days=14)
    assert removed["dry_runs_removed"] == 1
    assert store.get_run(old_dry) is None
    assert store.get_run(fresh_apply) is not None
