# Configuration

Unwatcharr splits configuration in two, deliberately.

- **Environment variables** carry only what must be known *before the database
  is open*: where `/config` is, which port, which timezone. Plus a few optional
  first-boot seeds.
- **Everything a user can change at runtime** — Plex connection, schedule, safe
  mode, notifications, retention, password — lives in the `settings` table. So it
  survives a container recreate, and a stale compose file can never silently
  clobber what you set in the browser.

---

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `CONFIG_DIR` | `./config` | Database and logs. `/config` in the image. |
| `HOST` | `0.0.0.0` | Bind address. |
| `PORT` | `8577` | Non-integer values fall back to 8577. |
| `TZ` | `UTC` | tz database name, e.g. `Europe/London`. Unknown ⇒ warning + system default, never a crash. |
| `LOG_LEVEL` | `info` | `debug` \| `info` \| `warning` \| `error`. Anything else ⇒ `info`. |
| `PUID` | `568` | Owner uid for `/config`. Entrypoint only. |
| `PGID` | `568` | Owner gid for `/config`. Entrypoint only. |

### First-boot seeds

Applied **only when the setting is still empty**, i.e. on first boot. Editing
them later does nothing — that is the point.

| Variable | Effect |
|---|---|
| `PLEX_URL` | Pre-seeds the server address, e.g. `http://192.168.1.10:32400`. |
| `PLEX_TOKEN` | Pre-seeds the owner's token. Leave unset to use the plex.tv link code instead — recommended. |

Derived paths: `DB_PATH = $CONFIG_DIR/unwatcharr.db`, `LOG_DIR = $CONFIG_DIR/logs`.

---

## Settings (the `settings` table)

Edited from the Settings page or `POST /api/settings`. Defaults come from
`store.CONFIG_DEFAULTS`; anything absent from the table falls back there.

### Plex connection

| Key | Default | Notes |
|---|---|---|
| `plex_url` | `""` | Set by the wizard. |
| `plex_machine_id` | `""` | Server identity. |
| `plex_server_name` | `""` | Display only. |
| `plex_verify_ssl` | `false` | |
| `client_identifier` | `""` | Generated once; identifies this app to plex.tv. |
| `setup_complete` | `false` | Gates runs. |
| `plex_account_token` | `""` | **Secret.** The plex.tv *account* token, kept apart from the server-scoped tokens in `plex_users.token`. They are not interchangeable: plex.tv operations need the account one, the PMS wants the scoped one. |

### Scheduling

| Key | Default | Range |
|---|---|---|
| `schedule_enabled` | `true` | |
| `schedule_kind` | `interval` | `interval` \| `cron` |
| `schedule_hours` | `6` | 1–720 |
| `schedule_cron` | `0 4 * * *` | 5-field cron; blank resets to the default |
| `catch_up_missed_runs` | `true` | Runs once on boot if a scheduled run was missed |
| `last_scheduled_run_at` | `0` | Bookkeeping |

Every settings save calls `scheduler.reschedule()`. A new interval takes effect
immediately, not after a restart.

### Safety

| Key | Default | Notes |
|---|---|---|
| `safe_mode` | **`true`** | **Not settable via `POST /api/settings`.** It has its own endpoint requiring `confirm: true`, because it is the difference between "this app can change my Plex history" and "it cannot" — never a checkbox someone flips past in a long form. |
| `request_delay_ms` | `100` | 0–5000. Throttles Plex calls. |
| `server_side_filters` | `true` | Push filtering into Plex's query where possible. |

While safe mode is on, an `apply` run is downgraded to `dry` and the run row
records `mode = "dry"` — the record tells the truth about what happened, not
about what was asked for.

### Retention

| Key | Default | Range |
|---|---|---|
| `history_keep_days` | `365` | 1–3650 |
| `dry_run_keep_days` | `14` | 1–365 |

Only the scheduled tick prunes. Manual runs never delete history.

### Notifications

| Key | Default | Notes |
|---|---|---|
| `notify_enabled` | `false` | |
| `notify_kind` | `webhook` | `webhook` \| `discord` \| `ntfy` |
| `notify_url` | `""` | |
| `notify_on_dry_run` | `false` | |
| `notify_on_error_only` | `false` | |

Notifications are silent when a run changed nothing, and a failing notification
never fails a run. Test yours with `POST /api/settings/notify-test`.

### Logging

| Key | Default | Notes |
|---|---|---|
| `log_to_file` | `false` | Writes to `$CONFIG_DIR/logs`. The in-memory ring buffer (2000 records) always works and backs the Logs page. |

Tokens are redacted from every log line by `RedactingFormatter`; known secrets
are registered as they are discovered.

### Web UI

| Key | Default | Notes |
|---|---|---|
| `ui_password_hash` | `""` | **Secret.** scrypt. Empty ⇒ no password ⇒ the dashboard warns you. |
| `ui_password_salt` | `""` | **Secret.** |
| `session_secret` | `""` | **Secret.** Generated on first boot, never baked into the image. |
| `secure_cookies` | `false` | Turn on when behind HTTPS. |

Changing the password re-issues the current session and **kills every other
session** — the session carries a password stamp that stops matching.

### Secrets

`plex_account_token`, `ui_password_hash`, `ui_password_salt`, `session_secret`
are in `SECRET_CONFIG_KEYS`. `store.public_config()` strips all four, so
`GET /api/settings` cannot return them. A test asserts it.

---

## Rule fields

A rule is created and edited as JSON. Full request/response shapes in
[API.md](API.md).

| Field | Default | Notes |
|---|---|---|
| `name` | — | Required, truncated at 120 chars |
| `enabled` | `true` | |
| `media_type` | `movie` | `movie` \| `show`. Fixed per rule. |
| `library_ids` | — | **Required, at least one**, and all must be of `media_type`. Mixing types is refused with a message telling you to make a second rule. |
| `age_value` | `90` | 0 = "everything, regardless of age" |
| `age_unit` | `days` | `hours` \| `days` \| `weeks` \| `months` \| `years` |
| `min_view_count` | `1` | at least 1 |
| `skip_in_progress` | `true` | Leave items with a resume point alone |
| `skip_now_playing` | `true` | Leave whatever is playing right now alone |
| `clear_progress` | `false` | Also clear the resume point. **Off by default.** |
| `require_series_complete` | `true` (show only) | Only touch a show when every episode is watched |
| `tv_scope` | `episodes` (show only) | `episodes` \| `series` |
| `include_filters` | `[]` | `[{"field": "...", "value": "..."}]` |
| `exclude_filters` | `[]` | Same shape |

Filter fields: `collection`, `label`, `genre`, `title`.
**Exclude always beats include.** Show-level tags are inherited by episodes.

TV-only gates are **normalised away on movie rules** rather than stored as "on",
so the editor can never lie about what the engine will do.

`tv_scope=series` collapses episodes into one show-level action **only when every
episode of that show matched** — a partly-matched show stays as episodes.

### Cutoff semantics

`hours`, `days` and `weeks` are fixed durations and are DST-exact. `months` and
`years` are calendar arithmetic with day clamping — 31 March minus one month is
28 February. `age_value = 0` means everything qualifies.

### Per-user overrides

Each rule can carry per-user overrides: a different `age_value`/`age_unit`, or
`enabled: false` to opt a user out entirely. An **absent or empty** override set
never disables a rule for anyone (there is a regression test
for it). `GET /api/rules/{id}/thresholds` shows the resolved value per user.

---

## Skip reasons

Every non-matching item carries one, surfaced in previews and run details.

| Reason | Meaning |
|---|---|
| `matched` | Will be marked unwatched |
| `not_watched` | Never watched |
| `below_min_views` | Watched fewer times than the minimum |
| `too_recent` | Watched too recently |
| `no_watch_date` | Plex has no watch date for this item |
| `in_progress` | Partly watched (resume point set) |
| `now_playing` | Being played right now |
| `series_incomplete` | Series is not fully watched |
| `excluded` | Matched an exclude filter |
| `not_included` | Did not match any include filter |
| `already_unwatched` | Already unwatched |

---

## Users

`kind` is one of `owner`, `home`, `managed`, `shared`.

`owner`, `home` and `managed` are **auto-linkable** — Unwatcharr can mint a
server-scoped token for them (a Plex Home PIN may be required). `shared` users
are not: Plex offers no admin route to a shared user's token, so you must paste
one via `POST /api/users/{id}/token`.

A user with no working token is skipped entirely. Their history is never read
and never modified.

---

## Security notes

- Session cookie signed with `session_secret`, carrying a password stamp so
  changing the password invalidates every other session.
- Login is rate limited: 10 attempts per 15 minutes.
- Every API route runs an origin check; a cross-origin POST gets 403.
- `/healthz` is the only unauthenticated route.
- `/api/thumb` proxies artwork so a Plex token never reaches the browser. It has
  **two allowlists** — a path allowlist for media-server art (which rejects
  traversal such as `/library/../:/unscrobble`) and a host allowlist for plex.tv
  avatars (which rejects `plex.tv.evil.com`). Without them it would be an open
  proxy into whatever the container can reach.
- Run the app with **one worker**. The run lock, progress state and scheduler are
  process-local.
