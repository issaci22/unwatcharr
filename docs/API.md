# Unwatcharr JSON API

**This is the contract.** The bundled HTML UI is disposable and unstyled; it is
built on exactly these endpoints and has no privileged access to anything. A
designed frontend consumes this document and nothing else.

- Base path: `/api`
- Everything is JSON in, JSON out. `Content-Type: application/json`.
- Every route requires an authenticated session **and** passes an origin check.
  The single exception is `GET /healthz` (no `/api` prefix), which returns
  `text/plain`.
- Errors are always flat: `{"detail": "A sentence a human can act on."}`.

---

## Conventions

### Auth

A signed session cookie, obtained by `POST /login` (form-encoded `password`) on
the page router. The cookie carries a password stamp: changing the UI password
invalidates every other session immediately. `POST /logout` clears it.

If no UI password is set, every request is authorised — and `status.warnings`
carries an explicit warning about it.

Login is rate limited to **10 attempts per 15 minutes**.

### Origin check

Every `/api` route calls `guard_origin`. A cross-origin `POST` gets **403**. A
browser frontend must be served from the same origin.

### Status codes

| Code | Meaning |
|---|---|
| `200` | Success |
| `400` | Validation or Plex error — read `detail` |
| `401` | Not authenticated |
| `403` | Origin rejected |
| `404` | Object no longer exists |
| `409` | Confirmation required (only `POST /settings/safe-mode`) |
| `422` | Malformed body (FastAPI validation, also flattened to `detail`) |

### Timestamps

Every timestamp field ending `_at` is an **ISO-8601 string in the configured
timezone**, or `null`. Some payloads add a human `relative` sibling
("3 days ago") and a `duration` string ("1m 12s"). Durations also appear as
`duration_seconds` (integer) where a machine value is useful.

### The token rule

**No endpoint ever returns a Plex token.** User objects carry `linked` (bool) and
`token_status` instead. `GET /settings` runs `public_config()`, which strips
`plex_account_token`, `ui_password_hash`, `ui_password_salt` and `session_secret`.
An e2e test asserts no token string appears in any response body or rendered
page. Artwork is proxied through `/api/thumb` for the same reason.

---

## Vocabulary

`GET /api/schema` returns every enumeration the UI needs, so nothing has to be
hardcoded in a frontend:

```json
{
  "age_units":     ["hours", "days", "weeks", "months", "years"],
  "filter_fields": ["collection", "label", "genre", "title"],
  "media_types":   ["movie", "show"],
  "tv_scopes":     ["episodes", "series"],
  "notify_kinds":  ["webhook", "discord", "ntfy"],
  "schedule_kinds":["interval", "cron"],
  "user_kinds":    ["owner", "home", "managed", "shared"],
  "skip_reasons":  {"matched": "Will be marked unwatched", "...": "..."}
}
```

`skip_reasons` maps every reason code to display text. Render from this map;
never hardcode the strings.

---

# Objects

These shapes are produced by `app/web/viewmodels.py` and are identical wherever
they appear.

## User

```json
{
  "id": 3,
  "plex_id": "12345678",
  "title": "Alex",
  "username": "alex",
  "kind": "home",
  "protected": false,
  "enabled": true,
  "linked": true,
  "token_status": "ok",
  "token_checked_at": "2026-08-30T09:12:04-04:00",
  "auto_linkable": true,
  "thumb": "https://plex.tv/users/abc/avatar",
  "avatar_url": "/api/thumb?path=https://plex.tv/users/abc/avatar"
}
```

- `linked` — a token exists. `token_status` — whether Plex still accepts it
  (`ok` / `invalid` / unchecked). **Never a token.**
- `auto_linkable` — true for `owner`, `home`, `managed`. False for `shared`:
  Plex offers no admin route to a shared user's token, so the UI must show a
  paste-a-token field for those and a "Link" button for the rest.
- `avatar_url` is `null` when there is no `thumb`. Always load avatars through
  the proxy — a direct plex.tv fetch would need a token in the browser.

## Library

```json
{ "id": 2, "section_key": "3", "title": "Movies", "type": "movie" }
```

`type` is `movie` or `show`. Music libraries are unsupported and never appear.

## Rule

```json
{
  "id": 7,
  "name": "Old movies",
  "enabled": true,
  "media_type": "movie",
  "age_value": 90,
  "age_unit": "days",
  "threshold": "90 days",
  "min_view_count": 1,
  "require_series_complete": false,
  "skip_in_progress": true,
  "skip_now_playing": true,
  "clear_progress": false,
  "tv_scope": "episodes",
  "include_filters": [{"field": "collection", "value": "Comfort"}],
  "exclude_filters": [{"field": "label", "value": "Keep"}],
  "libraries": [{"id": 2, "title": "Movies", "type": "movie"}],
  "custom_users": 1,
  "excluded_users": 0,
  "updated_at": "2026-08-30T09:00:00-04:00",
  "user_overrides": [
    {"user_id": 3, "enabled": true, "age_value": 30, "age_unit": "days"}
  ]
}
```

- `threshold` is a pre-rendered display string; `age_value` + `age_unit` are the
  editable truth.
- `user_overrides` is present only on single-rule endpoints (`GET`, `POST`,
  `PATCH /rules/{id}`), not in the list.
- `custom_users` / `excluded_users` are counts for the list view.
- On a `movie` rule, `require_series_complete` and `tv_scope` are normalised to
  their inert values — do not offer those controls for movies.

## Action (a history row)

```json
{
  "id": 991, "run_id": 40, "pass_id": 62,
  "rule_name": "Old movies", "user_title": "Alex", "user_id": 3,
  "rating_key": "45211", "item_type": "episode",
  "title": "The Constant", "grandparent_title": "Lost",
  "season": 4, "episode": 5, "year": null,
  "display_title": "Lost S04E05 - The Constant",
  "thumb": "/library/metadata/45211/thumb/1",
  "poster_url": "/api/thumb?path=/library/metadata/45211/thumb/1",
  "last_viewed_at": "2024-02-11T21:40:00-05:00",
  "last_viewed_relative": "1 year ago",
  "view_count_before": 2,
  "status": "applied",
  "error": null,
  "applied_at": "2026-08-30T04:00:11-04:00",
  "undone_at": null,
  "undoable": true
}
```

`status` is `applied`, `undone`, `failed` or `candidate` (dry-run only).
`undoable` is true only while `status == "applied"`.
`display_title` is pre-formatted: `Show SxxEyy - Title`, or `Title (Year)`.

## Run

```json
{
  "id": 40, "uid": "a1b2c3d4e5f6",
  "mode": "apply", "trigger": "manual", "status": "finished",
  "rules_processed": 2, "users_processed": 3,
  "scanned": 1204, "matched": 31, "applied": 31, "failed": 0, "skipped": 1173,
  "error": null,
  "started_at": "2026-08-30T04:00:00-04:00",
  "finished_at": "2026-08-30T04:01:12-04:00",
  "relative": "6 hours ago",
  "duration": "1m 12s",
  "duration_seconds": 72,
  "changed_anything": true
}
```

- `mode` is `dry` or `apply`. **This records what actually happened.** If safe
  mode downgraded an apply run, the row says `dry`.
- `trigger` is `manual` or `scheduled`.
- `status` is `running`, `finished`, `failed` or `cancelled`.
- **`changed_anything`** = `mode == "apply" && applied > 0`. This is the
  distinction the UI must never blur: a dry run with `matched: 31` changed
  nothing at all.

## RunPass (one rule × one user)

```json
{
  "id": 62, "run_id": 40,
  "rule_id": 7, "rule_name": "Old movies",
  "user_id": 3, "user_title": "Alex",
  "status": "finished",
  "scanned": 402, "matched": 12, "applied": 12, "failed": 0, "skipped": 390,
  "skip_summary": [{"reason": "too_recent", "count": 355},
                   {"reason": "not_watched", "count": 35}],
  "error": null,
  "duration": "24s"
}
```

## Log record

```json
{ "timestamp": "2026-08-30T09:15:22-04:00", "level": "INFO",
  "logger": "app.engine.runner", "message": "Run 40 finished" }
```

Messages are already redacted — no token can appear here.

---

# Endpoints

## Health

### `GET /healthz`
Unauthenticated. `text/plain`. Used by the container `HEALTHCHECK`.

## Status

### `GET /api/status`
The one dashboard payload. Poll this.

```json
{
  "app": {"name": "Unwatcharr", "version": "2.1.0",
          "schema_version": 1, "timezone": "America/New_York"},
  "setup_complete": true,
  "safe_mode": true,
  "plex": {"server_name": "Tower", "url": "http://192.168.1.10:32400",
           "machine_id": "abc123", "connected": true},
  "schedule": {"enabled": true, "kind": "interval", "hours": 6,
               "cron": "0 4 * * *", "running": true,
               "next_run_at": "2026-08-30T16:00:00-04:00",
               "last_scheduled_run_at": "2026-08-30T04:00:00-04:00"},
  "run": null,
  "last_run": { "...": "a Run object" },
  "stats": {"applied_total": 812, "applied_7d": 31, "applied_30d": 140,
            "undone_total": 4, "rules_total": 3, "rules_enabled": 2,
            "users_linked": 3},
  "users": {"total": 4, "linked": 3, "unlinked": 1, "expired": 0,
            "single_user": false},
  "warnings": [{"level": "warn", "message": "No web UI password is set. ..."}]
}
```

- `run` is the live progress object while a run is in flight, otherwise `null`.
- `warnings[].level` is `info`, `warn` or `err`. Render them; they are the app's
  way of telling the user something is wrong (no password, no enabled rules,
  unlinked users, rejected tokens, setup unfinished).
- `users.single_user` — when true, the UI can collapse all per-user affordances.

### `GET /api/schema`
The vocabulary block shown above.

---

## Setup

### `POST /api/setup/pin`
Start the plex.tv link-code flow. Returns the PIN object — show `code` to the
user, keep `id` for polling.

### `GET /api/setup/pin/{pin_id}`
Poll it. Before approval: `{"authorised": false}`. After:

```json
{ "authorised": true, "pin_id": "1234567",
  "servers": [{"name": "Tower", "machine_id": "abc123",
               "owned": true, "addresses": ["http://192.168.1.10:32400"]}] }
```

**The account token is never returned.** It is held server-side against the pin
id, which is a lookup key, not a secret. Pending entries expire after 15 minutes.

### `POST /api/setup/server`
Body: `{"pin_id": "...", "machine_id": "...", "name": "optional override"}`.
Probes every address Plex advertises for that server until one answers from
inside the container, then connects, refreshes libraries and users, and marks
setup complete. Returns:

```json
{ "library_count": 2, "server_name": "Tower",
  "plex_url": "http://192.168.1.10:32400",
  "total": 4, "linked": 1, "unlinked": 3, "shared_unlinked": 2,
  "single_user": false }
```

`400` when the sign-in expired, the server vanished from the account, or no
address was reachable — the last of which says so and points at manual setup.

### `POST /api/setup/manual`
Body: `{"url": "http://192.168.1.10:32400", "token": "..."}`. Same response
shape. Use when Plex's advertised addresses are not routable from the container.

---

## Libraries

### `GET /api/libraries?media_type=movie`
`media_type` optional. `{"libraries": [Library, ...]}`

### `POST /api/libraries/refresh`
Re-reads sections from Plex. `{"count": 2, "libraries": [Library, ...]}`

### `GET /api/libraries/{library_id}/tags/{field}`
`field` must be `collection`, `label` or `genre` (not `title`). Returns the
distinct values in that section, for populating a filter autocomplete.
`{"tags": ["Comfort", "Christmas", "..."]}`

---

## Rules

### `GET /api/rules`
`{"rules": [Rule, ...]}` — without `user_overrides`.

### `POST /api/rules`
Body: the rule fields. `library_ids` is required and must contain at least one
library, all of `media_type`. Returns the created Rule **with** `user_overrides`.

```json
{ "name": "Old movies", "media_type": "movie", "library_ids": [2],
  "age_value": 90, "age_unit": "days", "min_view_count": 1,
  "skip_in_progress": true, "skip_now_playing": true, "clear_progress": false,
  "include_filters": [], "exclude_filters": [],
  "user_overrides": [{"user_id": 3, "enabled": true,
                      "age_value": 30, "age_unit": "days"}] }
```

Validation failures return `400` with a sentence, e.g. *"A movie rule cannot
scan TV Shows. Pick libraries of one media type, or create a second rule."*

### `GET /api/rules/{id}` · `PATCH /api/rules/{id}`
`PATCH` is a partial update — omitted fields keep their current value, including
`library_ids`. Both return a Rule with `user_overrides`. `404` if gone.

### `DELETE /api/rules/{id}` → `{"deleted": 7}`

### `POST /api/rules/{id}/toggle` → the updated Rule

### `GET /api/rules/{id}/thresholds`
The resolved timer per user, after override inheritance.

```json
{"thresholds": [{"user_id": 3, "user_title": "Alex", "enabled": true,
                 "age_value": 30, "age_unit": "days",
                 "threshold": "30 days", "source": "override"}]}
```

### `POST /api/rules/{id}/preview?user_id=3`
**Ephemeral.** Evaluates one rule against one user's live Plex state. Writes
nothing to Plex, records no run, sends no notification, and never appears in
history. This is the endpoint the UI should push people toward before they touch
safe mode.

```json
{
  "rule_id": 7, "rule_name": "Old movies",
  "user_id": 3, "user_title": "Alex",
  "media_type": "movie", "threshold": "90 days",
  "libraries": ["Movies"],
  "scanned": 402, "matched": 12, "skipped": 390,
  "skip_summary": [{"reason": "too_recent", "count": 355}],
  "would_change": [ { "...": "PreviewItem" } ],
  "left_alone":   [ { "...": "PreviewItem" } ],
  "truncated": false,
  "error": null
}
```

A **PreviewItem**:

```json
{ "rating_key": "45211", "title": "The Constant",
  "display_title": "Lost S04E05 - The Constant", "item_type": "episode",
  "grandparent_title": "Lost", "season": 4, "episode": 5, "year": null,
  "thumb": "/library/metadata/45211/thumb/1",
  "last_viewed_at": 1707698400, "view_count": 2,
  "matched": false, "reason": "too_recent",
  "reason_text": "Watched too recently",
  "detail": "Watched 12 days ago; the timer is 90 days." }
```

Note `last_viewed_at` here is a **raw epoch integer**, not an ISO string —
preview items come from the engine, not the history serialiser.

`400` when the rule or user is gone, or the user has no token.

---

## Users

### `GET /api/users`
```json
{ "users": [User, ...],
  "summary": {"total": 4, "linked": 3, "unlinked": 1,
              "shared_unlinked": 1, "single_user": false} }
```

### `POST /api/users/refresh`
Re-reads users from three sources (server accounts, Plex Home, shared users).
Same shape as above.

### `POST /api/users/{id}/link`
Body: `{"pin": "1234"}` — the Plex Home PIN, if that user has one. Mints a
server-scoped token. Only valid for `auto_linkable` users.
`{"linked": "Alex", "users": [User, ...]}`

### `POST /api/users/{id}/token`
Body: `{"token": "..."}`. The only route for `shared` users. The token is
validated against Plex before it is stored, and is never echoed back.
`{"linked": "Sam", "users": [User, ...]}`

### `POST /api/users/{id}/toggle` → the updated User
Disabled users are skipped by every run.

### `DELETE /api/users/{id}` → `{"deleted": 3, "users": [User, ...]}`

---

## Runs

### `POST /api/runs`
Starts a run **in the background** and returns immediately.

Body (all optional): `{"mode": "apply", "rule_ids": [7], "user_ids": [3]}`.
`mode` is `dry` (default) or `apply`. Omitting `rule_ids`/`user_ids` means all
enabled ones.

```json
{ "run_id": 41, "uid": "f4912b4f2191", "mode": "apply", "trigger": "manual",
  "safe_mode": true, "effective_mode": "dry" }
```

**Read `effective_mode`, not `mode`.** When safe mode is on, an `apply` request
is downgraded to `dry`, and the finished run row will also say `dry`. The UI
must show what will actually happen, not what was asked for.

`400` when a run is already in progress, setup is unfinished, or `mode` is
invalid.

### `GET /api/runs/current`
Live progress, or `{"busy": false}`.

```json
{ "busy": true, "run_id": 41, "uid": "f491...", "mode": "dry",
  "trigger": "manual", "phase": "scanning",
  "done": 2, "total": 6, "percent": 33,
  "current": "Old movies / Alex" }
```

### `POST /api/runs/cancel`
Cooperative — the run finishes its current item and halts.
`{"cancelling": true}`, or `false` when nothing is running.

### `GET /api/runs?limit=25&offset=0`
`limit` 1–200. `{"runs": [Run, ...], "total": 40, "limit": 25, "offset": 0}`

### `GET /api/runs/{id}`
A Run, plus `passes` (array of RunPass) and `undoable` (count of actions that can
still be undone). `404` if gone.

### `GET /api/runs/{id}/items?limit=500`
`limit` 1–2000. `{"items": [Action, ...]}` — every row this run touched,
including dry-run candidates (capped at `DRY_RUN_ROW_LIMIT = 1000` when written).

### `POST /api/runs/{id}/undo`
```json
{"undone": 31, "failed": 0, "caveat": "Undo marks the item watched again. Plex records a new play, so the original watch date and play count cannot be restored."}
```

---

## History

### `GET /api/history`
Query: `limit` (1–200, default 50), `offset`, `status`, `user_id`, `rule_id`,
`search`.

```json
{ "actions": [Action, ...], "total": 812, "limit": 50, "offset": 0,
  "caveat": "Undo marks the item watched again. ..." }
```

Dry-run candidates are **excluded** from history. History is only about things
that really happened.

### `POST /api/actions/{id}/undo`
`{"undone": 1, "failed": 0, "caveat": "..."}`

> **Surface the caveat every time you offer undo.** Undo re-scrobbles: Plex
> records a fresh play, so the original watch date and play count are lost. The
> API returns the exact sentence to display so it cannot drift.

---

## Settings

### `GET /api/settings`
```json
{ "settings": { "...": "public_config(), secrets stripped" },
  "schema":   { "...": "the same block as GET /api/schema" } }
```

### `POST /api/settings`
Partial. Only keys present in the body are written; unknown keys are ignored.
Always calls `scheduler.reschedule()`.

Integers are **clamped, not rejected**:

| Key | Default | Min | Max |
|---|---|---|---|
| `request_delay_ms` | 100 | 0 | 5000 |
| `schedule_hours` | 6 | 1 | 720 |
| `history_keep_days` | 365 | 1 | 3650 |
| `dry_run_keep_days` | 14 | 1 | 365 |

A non-numeric value is a `400`. Booleans accepted: `schedule_enabled`,
`catch_up_missed_runs`, `server_side_filters`, `plex_verify_ssl`,
`notify_enabled`, `notify_on_dry_run`, `notify_on_error_only`, `log_to_file`,
`secure_cookies`. Enums (`400` on a bad value): `schedule_kind`
(`interval`/`cron`), `notify_kind` (`webhook`/`discord`/`ntfy`). Free strings:
`schedule_cron` (blank resets to `0 4 * * *`), `notify_url`.

**`safe_mode` is deliberately not settable here.**

Returns `{"settings": {...}, "next_run_at": "..."}`.

### `POST /api/settings/safe-mode`
Body: `{"enabled": false, "confirm": true}`.

Turning safe mode **off** without `confirm: true` returns **409**:

> "Turning safe mode off lets runs really mark items unwatched in Plex. Send
> confirm: true once you have reviewed a preview."

Turning it **on** never needs confirmation. Returns `{"safe_mode": false}`.

This is the one destructive toggle in the app. It gets its own endpoint, its own
confirmation and its own warning log line precisely so it can never be a
checkbox in a long form that someone tabs past.

### `POST /api/settings/test-connection` → `{"message": "..."}`

### `POST /api/settings/notify-test`
Body: `{"notify_kind": "...", "notify_url": "..."}` — both optional, falling back
to the saved settings. `400` when there is no URL or the endpoint rejected it.

### `POST /api/settings/password`
Body: `{"password": "..."}`. At least 6 characters. An **empty** password removes
the password entirely, logs you out, and returns a blunt warning:

```json
{"password_set": false,
 "message": "Password removed. Anyone who can reach this port can now change your Plex watch history."}
```

Setting a password re-issues the current session and kills every other one.

---

## Logs and artwork

### `GET /api/logs?limit=200&level=DEBUG&search=`
`limit` 1–2000. Reads the in-memory ring buffer (2000 records), which works
whether or not file logging is on.
`{"logs": [LogRecord, ...], "levels": ["DEBUG", "INFO", "WARNING", "ERROR"]}`

### `GET /api/thumb?path=...`
Returns image bytes. Two accepted shapes:

- a media-server path, `/library/metadata/123/thumb/1`
- an absolute plex.tv avatar URL

Each has its own allowlist — a path allowlist that rejects traversal (e.g.
`/library/../:/unscrobble`) and a host allowlist that rejects lookalikes
(e.g. `plex.tv.evil.com`). Anything else gets a `404` with no body. Without those
this endpoint would be an open proxy into whatever the container can reach.

Use the `poster_url` / `avatar_url` fields the API already gives you rather than
constructing this URL yourself.

---

# Notes for a frontend

1. **Poll `GET /api/status`** for the dashboard, and `GET /api/runs/current`
   while `status.run` is non-null. There is no websocket.
2. **Never blur dry and apply.** `run.changed_anything` exists for exactly this.
   A dry run that "matched 400 items" changed nothing.
3. **Read `effective_mode` from `POST /api/runs`**, not the mode you sent.
4. **Always show the undo caveat** returned alongside every undo result.
5. **Render skip reasons from `schema.skip_reasons`.** They are the app's main
   explanatory surface: they are why a user trusts the preview.
6. **Drive all vocabulary from `GET /api/schema`.** New units, filter fields or
   notification kinds should not require a frontend change.
7. **Artwork only through `/api/thumb`.** A direct Plex fetch would need a token
   in the browser, and the whole design exists to prevent that.
8. **Errors are always `{"detail": "..."}`** and the sentence is already written
   for a human. Show it verbatim rather than mapping status codes to your own
   copy.
9. **`409` means "ask again with confirmation"**, and today only comes from
   `POST /settings/safe-mode`.
10. **Respect `users.single_user`** — with one user, per-user pickers, override
    tables and threshold breakdowns are noise.
