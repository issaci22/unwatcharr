"""End-to-end tests against a real app process and a token-aware mock Plex.

Assertions read the mock's `/__calls` and `/__state` rather than the app's own
summary — the question is always what actually reached Plex, and with which
token.
"""

from __future__ import annotations

import json

import httpx
import pytest


def make_rule(client: httpx.Client, **over) -> dict:
    libraries = client.get("/api/libraries").json()["libraries"]
    media_type = over.pop("media_type", "movie")
    wanted = [l for l in libraries if l["type"] == media_type]
    body = {
        "name": over.pop("name", f"{media_type} rule"),
        "media_type": media_type,
        "age_value": over.pop("age_value", 90),
        "age_unit": over.pop("age_unit", "days"),
        "library_ids": over.pop("library_ids", [wanted[0]["id"]]),
    }
    body.update(over)
    response = client.post("/api/rules", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def owner_id(client: httpx.Client) -> int:
    users = client.get("/api/users").json()["users"]
    return next(u["id"] for u in users if u["linked"])


# ---------------------------------------------------------------------------
# Setup and pages
# ---------------------------------------------------------------------------

def test_setup_discovers_libraries_and_users(client, db):
    libraries = client.get("/api/libraries").json()["libraries"]
    titles = sorted(l["title"] for l in libraries)
    # Music has no watch state and must never be offered.
    assert titles == ["Movies", "TV Shows"], titles

    users = client.get("/api/users").json()["users"]
    assert users, "setup should have discovered at least the owner"
    assert any(u["kind"] == "owner" for u in users)


PAGES = ["/", "/rules", "/users", "/history", "/logs", "/settings"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(client, path):
    response = client.get(path, follow_redirects=True)
    assert response.status_code == 200, path
    assert "Unwatcharr" in response.text


@pytest.mark.parametrize("path", PAGES)
def test_shell_has_no_full_width_mode_banner(client, path):
    """The mode banner was four-way redundant; the sidebar chip carries it now.

    Checked in both modes, because the banner had a state for each and removing
    only the loud one would have left safe mode shouting louder than the mode
    that can actually modify Plex.
    """
    for enabled in (False, True):
        client.post(
            "/api/settings/safe-mode", json={"enabled": enabled, "confirm": True}
        )
        html = client.get(path, follow_redirects=True).text
        assert "modebanner" not in html, (path, enabled)
    client.post("/api/settings/safe-mode", json={"enabled": False, "confirm": True})


@pytest.mark.parametrize("path", PAGES)
def test_sidebar_keeps_the_mode_indicator(client, path):
    """The chip is now the primary persistent indicator, so it is not optional.

    Icon + word + colour: the class carries the colour, the word is in the
    markup, and the tooltip says what the mode means.
    """
    client.post("/api/settings/safe-mode", json={"enabled": False, "confirm": True})
    html = client.get(path, follow_redirects=True).text
    assert "modechip modechip--active" in html, path
    assert "Active" in html and "mode on" in html, path
    assert "Active mode: runs can change Plex" in html, path

    client.post("/api/settings/safe-mode", json={"enabled": True, "confirm": True})
    html = client.get(path, follow_redirects=True).text
    assert "modechip modechip--safe" in html, path
    assert "Safe mode: nothing in Plex is changed" in html, path
    client.post("/api/settings/safe-mode", json={"enabled": False, "confirm": True})


@pytest.mark.parametrize("path", PAGES)
def test_sidebar_toggle_is_a_labelled_hamburger(client, path):
    """The control was a bare X that did nothing above 960px.

    It is one button for one navigation: `data-drawer-close` for the phone
    drawer and `data-nav-toggle` for the desktop rail, with the accessible name
    and `aria-expanded` that a disclosure control owes a screen reader.
    """
    html = client.get(path, follow_redirects=True).text
    assert "data-nav-toggle" in html, path
    assert 'aria-controls="sidenav"' in html, path
    assert 'aria-expanded="true"' in html, path
    assert "Collapse navigation" in html, path
    # The hamburger from _icons.html, not the close X it replaced.
    assert "M3.5 6.5h17M3.5 12h17M3.5 17.5h17" in html, path
    assert "sidenav__close" not in html, path


def test_rule_editor_error_starts_hidden(client):
    """The error callout is `display: flex` by class, so it must be BOTH marked
    hidden in the markup and swept by `.callout[hidden]` in app.css — otherwise
    it renders as an empty red bar with an icon and no sentence in it.

    The component is not deleted: it is still in the page, ready to be filled.
    """
    html = client.get("/rules", follow_redirects=True).text
    assert 'id="editor-error"' in html
    assert 'id="editor-error-text"' in html
    marker = html.index('id="editor-error"')
    assert "hidden" in html[marker:marker + 120], html[marker:marker + 120]

    # Order is the whole fix: an author `display` rule beats the UA sheet's
    # `[hidden]` at any specificity, so the sweep only works if it comes after
    # the `.callout { display: flex }` that was overriding it.
    css = client.get("/static/app.css").text
    assert ".callout[hidden]" in css
    assert css.index(".callout[hidden]") > css.index(".callout {")
    assert "display: none !important" in css[css.index(".callout[hidden]"):][:200]


@pytest.mark.parametrize("path", PAGES)
def test_static_assets_are_cache_busted(client, path):
    """The shell must never link `/static/app.css` at a URL that cannot change.

    Both fixes above are in the files, and both were still on screen after the
    upgrade that shipped them: Starlette sends an ETag but no `Cache-Control`,
    so the browser invented a freshness lifetime and kept the previous
    release's CSS and JS for hours without asking. The failure mode is the
    nastiest kind -- last version's bugs, in a build that does not contain
    them, on one machine.
    """
    html = client.get(path, follow_redirects=True).text
    for asset in ("theme.css", "app.css", "app.js"):
        assert f"/static/{asset}?v=" in html, (path, asset)


def test_static_cache_headers_are_explicit(client):
    """Versioned URLs are immutable for a year; a bare one must revalidate.

    `no-cache` is not `no-store`: the file is still cached, the ETag still
    answers with a 304 and no body, but the browser can no longer decide on its
    own to skip the question.
    """
    html = client.get("/rules", follow_redirects=True).text
    start = html.index("/static/app.css?v=")
    versioned = html[start:html.index('"', start)]

    fresh = client.get(versioned)
    assert fresh.status_code == 200
    assert fresh.headers["cache-control"] == "public, max-age=31536000, immutable"

    bare = client.get("/static/app.css")
    assert bare.headers["cache-control"] == "no-cache"

    revalidated = client.get(
        "/static/app.css", headers={"If-None-Match": bare.headers["etag"]}
    )
    assert revalidated.status_code == 304
    assert revalidated.content == b""


def test_healthz_needs_no_auth(app_server):
    assert httpx.get(f"{app_server['url']}/healthz", timeout=10).text == "ok"


def test_status_reports_connection_and_safe_mode(client):
    status = client.get("/api/status").json()
    assert status["setup_complete"] is True
    assert status["plex"]["connected"] is True
    assert status["safe_mode"] is False
    assert status["app"]["name"] == "Unwatcharr"


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def test_rule_spans_multiple_libraries(client):
    libraries = client.get("/api/libraries").json()["libraries"]
    movies = [l for l in libraries if l["type"] == "movie"]
    rule = make_rule(client, library_ids=[l["id"] for l in movies])
    assert len(rule["libraries"]) == len(movies)


def test_rule_rejects_a_library_of_the_wrong_type(client):
    libraries = client.get("/api/libraries").json()["libraries"]
    tv = next(l for l in libraries if l["type"] == "show")
    response = client.post(
        "/api/rules",
        json={"name": "Wrong", "media_type": "movie", "library_ids": [tv["id"]]},
    )
    assert response.status_code == 400
    assert "media type" in response.json()["detail"].lower()


def test_movie_rule_normalises_tv_only_gates(client):
    rule = make_rule(client, require_series_complete=True, tv_scope="series")
    assert rule["require_series_complete"] is False
    assert rule["tv_scope"] == "episodes"


def test_opening_the_editor_creates_nothing(client, db):
    """v1 trap: creating the row up front left orphan rules behind Cancel."""
    before = len(db("SELECT id FROM rules"))
    client.get("/rules", follow_redirects=True)
    client.get("/api/schema")
    assert len(db("SELECT id FROM rules")) == before


def test_editing_a_rule_does_not_clone_it(client, db):
    rule = make_rule(client, name="Original")
    client.patch(f"/api/rules/{rule['id']}", json={"name": "Renamed", "media_type": "movie",
                                                   "library_ids": [l["id"] for l in rule["libraries"]]})
    rows = db("SELECT id, name FROM rules")
    assert len(rows) == 1 and rows[0]["name"] == "Renamed"


def test_toggle_and_delete(client, db):
    rule = make_rule(client)
    assert client.post(f"/api/rules/{rule['id']}/toggle").json()["enabled"] is False
    assert client.post(f"/api/rules/{rule['id']}/toggle").json()["enabled"] is True
    assert client.delete(f"/api/rules/{rule['id']}").status_code == 200
    assert db("SELECT id FROM rules") == []
    assert client.delete(f"/api/rules/{rule['id']}").status_code == 404


# ---------------------------------------------------------------------------
# Safe mode
# ---------------------------------------------------------------------------

def test_safe_mode_downgrades_apply_to_dry(client, plex_calls, wait_for_run):
    make_rule(client)
    client.post("/api/settings/safe-mode", json={"enabled": True, "confirm": True})

    started = client.post("/api/runs", json={"mode": "apply"}).json()
    assert started["effective_mode"] == "dry"
    run = wait_for_run(client)

    assert run["mode"] == "dry", "the run row must record the downgrade"
    assert plex_calls() == [], "safe mode must never let a write reach Plex"


def test_safe_mode_off_requires_confirmation(client):
    client.post("/api/settings/safe-mode", json={"enabled": True, "confirm": True})
    response = client.post("/api/settings/safe-mode", json={"enabled": False})
    assert response.status_code == 409
    assert client.get("/api/status").json()["safe_mode"] is True


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def test_dry_run_changes_nothing(client, plex_calls, wait_for_run):
    make_rule(client)
    client.post("/api/runs", json={"mode": "dry"})
    run = wait_for_run(client)
    assert run["matched"] > 0
    assert plex_calls() == []


def test_apply_unwatches_exactly_the_right_items(client, plex_calls, plex_state, wait_for_run):
    make_rule(client, age_value=90, age_unit="days")
    client.post("/api/runs", json={"mode": "apply"})
    run = wait_for_run(client)

    calls = plex_calls()
    assert calls, "apply must actually reach Plex"
    assert all(c["call"] == "unscrobble" for c in calls)
    assert run["applied"] == len(calls)

    state = plex_state()["tok-owner"]
    for call in calls:
        assert state[call["key"]]["viewCount"] == 0
        assert state[call["key"]]["lastViewedAt"] is None


def test_second_run_is_a_no_op(client, plex_calls, wait_for_run):
    make_rule(client)
    client.post("/api/runs", json={"mode": "apply"})
    wait_for_run(client)
    first = len(plex_calls())
    assert first > 0

    client.post("/api/runs", json={"mode": "apply"})
    second = wait_for_run(client)
    assert second["applied"] == 0
    assert len(plex_calls()) == first


def test_clear_progress_is_off_by_default(client, plex_calls, wait_for_run):
    make_rule(client)
    client.post("/api/runs", json={"mode": "apply"})
    wait_for_run(client)
    assert not any(c["call"] == "progress" for c in plex_calls())


def test_run_can_target_one_rule(client, plex_calls, wait_for_run):
    movies = make_rule(client, name="Movies", media_type="movie")
    make_rule(client, name="TV", media_type="show")

    client.post("/api/runs", json={"mode": "apply", "rule_ids": [movies["id"]]})
    run = wait_for_run(client)
    assert run["rules_processed"] == 1


def test_run_detail_exposes_passes_and_skip_reasons(client, wait_for_run):
    make_rule(client)
    client.post("/api/runs", json={"mode": "dry"})
    run = wait_for_run(client)

    detail = client.get(f"/api/runs/{run['id']}").json()
    assert detail["passes"], "a run must record a pass per rule x user"
    reasons = detail["passes"][0]["skip_summary"]
    assert reasons and all({"reason", "count"} <= set(r) for r in reasons)


def test_series_gate_protects_a_partly_watched_show(client, plex_calls, wait_for_run):
    make_rule(client, media_type="show", require_series_complete=True)
    client.post("/api/runs", json={"mode": "apply"})
    wait_for_run(client)

    touched = {c["key"] for c in plex_calls()}
    # 3001 is the partly-watched series in the fixtures.
    severance = {"3101", "3102"}
    assert not (touched & severance), f"partly-watched series was touched: {touched}"


# ---------------------------------------------------------------------------
# Preview — ephemeral
# ---------------------------------------------------------------------------

def test_preview_writes_nothing_and_explains_skips(client, db, plex_calls):
    rule = make_rule(client)
    runs_before = len(db("SELECT id FROM runs"))

    preview = client.post(
        f"/api/rules/{rule['id']}/preview?user_id={owner_id(client)}"
    ).json()

    assert len(db("SELECT id FROM runs")) == runs_before, "preview must not record a run"
    assert plex_calls() == [], "preview must not write to Plex"
    assert preview["matched"] > 0
    assert preview["skipped"] > 0
    assert preview["would_change"] and preview["left_alone"]
    # The distinction the UI has to make obvious.
    assert all(i["matched"] for i in preview["would_change"])
    assert all(not i["matched"] and i["reason_text"] for i in preview["left_alone"])


def test_preview_needs_a_linked_user(client):
    rule = make_rule(client)
    response = client.post(f"/api/rules/{rule['id']}/preview?user_id=99999")
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# History and undo
# ---------------------------------------------------------------------------

def test_history_lists_applied_changes(client, wait_for_run):
    make_rule(client)
    client.post("/api/runs", json={"mode": "apply"})
    run = wait_for_run(client)

    history = client.get("/api/history").json()
    assert history["total"] == run["applied"]
    assert all(a["status"] == "applied" for a in history["actions"])
    assert "cannot be restored" in history["caveat"]


def test_dry_run_candidates_are_not_history(client, wait_for_run):
    make_rule(client)
    client.post("/api/runs", json={"mode": "dry"})
    wait_for_run(client)
    assert client.get("/api/history").json()["total"] == 0


def test_undo_a_whole_run(client, plex_calls, plex_state, wait_for_run):
    make_rule(client)
    client.post("/api/runs", json={"mode": "apply"})
    run = wait_for_run(client)

    result = client.post(f"/api/runs/{run['id']}/undo").json()
    assert result["undone"] == run["applied"] and result["failed"] == 0

    assert {c["call"] for c in plex_calls()} == {"unscrobble", "scrobble"}
    state = plex_state()["tok-owner"]
    scrobbled = [c["key"] for c in plex_calls() if c["call"] == "scrobble"]
    for key in scrobbled:
        assert state[key]["viewCount"] > 0

    history = client.get("/api/history").json()
    assert all(a["status"] == "undone" for a in history["actions"])


def test_single_undo(client, plex_calls, wait_for_run):
    make_rule(client)
    client.post("/api/runs", json={"mode": "apply"})
    wait_for_run(client)

    action = client.get("/api/history").json()["actions"][0]
    result = client.post(f"/api/actions/{action['id']}/undo").json()
    assert result["undone"] == 1
    assert any(c["call"] == "scrobble" for c in plex_calls())


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

def test_no_plex_token_ever_reaches_a_response(client):
    """The invariant the whole app is built around."""
    endpoints = [
        "/api/status", "/api/users", "/api/rules", "/api/settings",
        "/api/history", "/api/logs", "/api/libraries", "/api/runs",
    ]
    for path in endpoints:
        body = client.get(path).text
        assert "tok-owner" not in body, f"token leaked from {path}"
        assert "plex_account_token" not in body, f"secret key exposed by {path}"

    for path in ["/", "/users", "/rules", "/settings", "/history"]:
        body = client.get(path, follow_redirects=True).text
        assert "tok-owner" not in body, f"token leaked from page {path}"


@pytest.mark.parametrize(
    "path",
    [
        "/:/unscrobble",
        "/status/sessions",
        "/library/../:/unscrobble",
        "http://evil.example/x",
        "https://evil.example/avatar",
    ],
)
def test_thumb_proxy_is_not_an_open_proxy(client, path):
    response = client.get("/api/thumb", params={"path": path})
    assert response.status_code in (400, 404), f"{path} should be refused"


def test_thumb_proxy_serves_real_artwork(client):
    response = client.get("/api/thumb", params={"path": "/library/metadata/101/thumb"})
    assert response.status_code == 200
    assert response.content == b"\x89PNG-mock-art"


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("GET", "/api/rules/999999", 404),
        ("GET", "/api/runs/999999", 404),
        ("DELETE", "/api/rules/999999", 404),
        ("POST", "/api/actions/999999/undo", 400),
        ("GET", "/api/libraries/999999/tags/genre", 400),
        ("GET", "/api/libraries/1/tags/bogus", 400),
    ],
)
def test_missing_things_fail_cleanly(client, method, path, expected):
    response = client.request(method, path)
    assert response.status_code == expected, response.text
    assert "detail" in response.json()


def test_cross_origin_writes_are_blocked(client):
    response = client.post(
        "/api/runs", json={"mode": "dry"}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert response.status_code == 403


def test_validation_errors_are_flat_strings(client):
    response = client.get("/api/history?limit=99999")
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_settings_round_trip_and_reschedule(client):
    response = client.post(
        "/api/settings",
        json={"schedule_enabled": True, "schedule_kind": "interval",
              "schedule_hours": 12, "history_keep_days": 30},
    )
    assert response.status_code == 200
    settings = response.json()["settings"]
    assert settings["schedule_hours"] == 12
    assert settings["history_keep_days"] == 30
    assert response.json()["next_run_at"], "saving must reschedule the job"


def test_settings_never_expose_secrets(client):
    settings = client.get("/api/settings").json()["settings"]
    for key in ("plex_account_token", "session_secret", "ui_password_hash",
                "ui_password_salt"):
        assert key not in settings


def test_settings_reject_bad_values(client):
    assert client.post("/api/settings", json={"schedule_kind": "hourly"}).status_code == 400
    assert client.post("/api/settings", json={"notify_kind": "carrier-pigeon"}).status_code == 400
    assert client.post("/api/settings", json={"schedule_hours": "soon"}).status_code == 400


def test_integer_settings_are_clamped(client):
    settings = client.post("/api/settings", json={"request_delay_ms": 999999}).json()["settings"]
    assert settings["request_delay_ms"] == 5000


def test_test_connection_reports_libraries(client):
    message = client.post("/api/settings/test-connection").json()["message"]
    assert "2 video libraries" in message
