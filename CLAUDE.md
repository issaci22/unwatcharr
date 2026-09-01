# CLAUDE.md — Unwatcharr engineering guide

## What this is

A sidecar container that marks old watched Plex media unwatched again, on a
schedule, driven by a web UI. Python 3.12 + FastAPI + SQLite + APScheduler.
Targets a TrueNAS box, so image size and RAM matter.

Plex removed plugin support in 2018 — this cannot run inside Plex and talks to
its HTTP API from outside.

## Commands

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

Run the app (writes to `./config` unless `CONFIG_DIR` is set; serves on 8577):

```bash
.venv/Scripts/python.exe -m app
```

Tests:

```bash
.venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/e2e   # fast, pure logic
.venv/Scripts/python.exe -m pytest tests/e2e -q                    # boots app + mock Plex
```

Ad-hoc scripts that import `app` need the repo root on the path:

```bash
PYTHONPATH=. .venv/Scripts/python.exe some_script.py
```

Mock Plex standalone:

```bash
.venv/Scripts/python.exe tests/e2e/mock_plex.py 32400
```

Docker:

```bash
docker build -t unwatcharr:latest .   # local build; not what users run
docker compose up -d                  # pulls ghcr.io/issaci22/unwatcharr:latest
```

No linter or formatter is configured.

## Documentation map

| File | What it is |
| --- | --- |
| [README.md](README.md) | Front door: what it does, quick start, architecture in one screen |
| [docs/INSTALL.md](docs/INSTALL.md) | Docker / TrueNAS / bare metal, PUID/PGID, first run, troubleshooting |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every env var and every `CONFIG_DEFAULTS` key, rule fields, skip reasons |
| [docs/API.md](docs/API.md) | **The design-phase handoff.** Every endpoint, every viewmodel field |
| [docs/PROJECT-BRIEF.md](docs/PROJECT-BRIEF.md) | The original brief. Requirements document — never delete |

When an endpoint, a viewmodel field or a `CONFIG_DEFAULTS` key changes,
`docs/API.md` and `docs/CONFIGURATION.md` change in the same commit. API.md is
the only thing a frontend build will read.

## Docker

Single stage on purpose: every dependency is a pure-Python or manylinux wheel,
so a builder stage saves almost nothing while adding a hardcoded
`/lib/python3.12/site-packages` copy path that breaks silently on a Python minor
bump. Image is **206 MB**.

- `setpriv` presence is checked **at build time**, so a base image without
  util-linux fails the build with a clear message rather than at first boot.
- The build runs an import check under `CONFIG_DIR=/tmp/importcheck`, then
  deletes it. Importing `app.main` opens the database and generates the session
  secret — without the throwaway directory, **every container from the image
  would ship the same baked secret**.
- `docker-entrypoint.sh` chowns `/config` **only when the ownership is actually
  wrong** (a recursive chown every boot is wasted I/O on a NAS), then
  `exec setpriv --reuid --regid --clear-groups`. It no-ops when already non-root.
- **One compose file: `docker-compose.yaml`.** It pulls
  `ghcr.io/issaci22/unwatcharr:latest` and never builds. The build, TrueNAS and
  prod variants were deleted — TrueNAS is now a two-line diff (dataset path,
  PUID/PGID 568) documented in INSTALL, and a local build means editing the
  `image:` line. README and `docs/INSTALL.md` embed this file's contents as a
  copy-paste block, so **any edit to it must be mirrored into both** — that is
  the one place the docs can silently drift from the tree.
- `.github/workflows/docker-publish.yml` builds amd64+arm64 on every push to
  `main` and on `v*.*.*` tags, pushes to GHCR with `GITHUB_TOKEN` (no secrets to
  configure), then smoke-tests the pushed digest against `/healthz`. It is the
  only thing that produces a published tag — never `docker push` by hand.

## The constraint everything is shaped by

**Plex watch state is per-account, and a token only reads and writes the watch
state of the account it belongs to.** There is no impersonation parameter.

Consequences that must never be "simplified away":

- `plex/client.py` takes `token` as a **per-call argument**, not client state —
  one run hits the same server with several users' tokens over one pool.
- The runner fans out to one `run_passes` row **per rule per user**, re-fetching
  the same library once per user, because `viewCount`/`viewedLeafCount` differ
  per token.
- Users split into two classes (`plex_users.kind`): owner/home/managed get a
  token minted through plex.tv's home-switch endpoint. **Shared** users (friends
  with their own Plex account) have no admin path to their token and must paste
  one. `shared_servers` sometimes carries a per-user `accessToken` — use it when
  present, keep the paste-a-token fallback working.
- Two stored tokens are **not interchangeable**: `plex_account_token` is the
  owner's plex.tv *account* token (plex.tv operations); `plex_users.token` holds
  **server-scoped** tokens used against the PMS.

## Architecture

```
HTTP ─┬─ web/api.py    (JSON: every read and every mutation)  ← the contract
      └─ web/pages.py  (server-rendered pages; initial state only)
              │
      web/viewmodels.py   (one set of dict builders, shared by both)
              │
         services/   setup · users · rules · runs · status
              │
    engine/  rules (PURE) · collect · preview · runner · scheduler
              │
          plex/  client · account · types
              │
             Plex

        store.py  ← ALL SQL. Called from services/engine only, never from routes.
```

**The JSON API is the single contract, and it is now frozen.** The backend is
feature-complete (140 unit + 50 e2e green). The `/design` phase is in progress:
`app/web/templates/`, `app/web/static/` and `app/web/pages.py` are the only
files it may touch. A new field goes in `viewmodels.py` and `docs/API.md` in the
same commit — never straight into a template.

- **`engine/rules.py` is pure** — no HTTP, no DB, no clock of its own. It takes
  fetched items plus an explicit `now` and returns a `Decision` per item.
- **Server-side Plex filters are an optimisation only.** Plex silently ignores a
  filter it does not recognise and returns the whole library, so every field is
  re-checked in `rules.py`. Never move a gate into the query and delete the
  local check.
- **`store.build_rule(row, override)` is the single place a per-user override is
  resolved** into an effective threshold. The engine has no idea per-user rules
  exist — keep it that way.
- **One uvicorn worker only.** The scheduler, the run lock and the SQLite
  connection are process-local; a second worker double-runs every rule.
- `python-plexapi` is deliberately not a dependency. No CDN assets — the NAS may
  have no outbound internet.

## Web UI

Server-rendered Jinja for initial state, vanilla JS for every mutation. No
framework, no build step, no CDN — the NAS may have no outbound internet, and
this runs beside Plex on modest hardware.

```
app/web/templates/base.html   the app shell: sidebar/drawer, status rail,
                              mode banner, toast region
app/web/templates/_icons.html icons.icon(name, size, cls) / icons.logo(size)
app/web/templates/_empty.html blank.state(glyph, title, text, steps) — THE empty
                              state; blank.filtered() for "your filter matched
                              nothing"
app/web/static/theme.css      design tokens — the ONLY file naming a raw colour
app/web/static/app.css        base layer, shell, components, responsive
app/web/static/app.js         api() + act() + toast() + shell behaviour
```

### Rules that hold the design together

- **No raw colour outside `theme.css`.** A component reads a semantic token
  (`--surface`, `--text-muted`, `--danger-tint`). A hex in `app.css` or a
  template is a bug, because it will not follow the light theme.
- **Dark is `:root`; light is a designed palette**, not an inversion, and the OS
  preference applies only until the user picks one. An inline script in `<head>`
  stamps the saved theme before first paint.
- **Colour is never the only signal.** Every status is icon + word + colour, or
  it fails for a colour-blind user and in a screenshot.
- **Safe mode is loud on every page.** A full-width mode banner in one of two
  states that do not look alike, plus a chip in the sidebar and the app bar.
  Never a checkbox in a long form — that is why it has its own endpoint.
- **Never blur dry and apply.** Read `effective_mode` from `POST /api/runs` and
  `changed_anything` on a run row. A dry run that matched 400 items changed
  nothing, and the UI must say so.
- **Mobile is a layout, not a squeeze.** Under 960px the sidebar becomes a
  drawer, and a wide table gets `.table--cards` + `data-label` attributes so it
  stacks instead of scrolling sideways.
- **Every state is designed**: loading (`data-busy` on a button keeps its label
  and gains a spinner), empty (say what to do next), and error (show the API's
  `detail` sentence verbatim — it is already written for a human).
- **Forms are server-rendered markup, not JS strings.** The rule editor renders
  its fields, labels and options from `schema` in the template; the script only
  fills them and reads them back. Half the code, real labels, no escaping
  surface. A `<template>` element covers the one repeatable row.
- **Validation is inline, next to the field** — a toast disappears while the
  user is still reading the form. Toasts are for background outcomes.
- `app.js` exposes `api`, `act`, `confirmAct`, `confirmDialog`, `toast`, `busy`,
  `startRun`, `startRunPolling`, `errorText`, `showError`. Page templates depend
  on those names; extend, do not rename. Deliberate actions use `confirmDialog`,
  not `window.confirm`.
- **A status code is never shown as a number.** `api()` prefers the endpoint's
  own `detail` sentence and otherwise maps the status through `HTTP_TEXT`;
  `errorText(err)` is the single place an error becomes readable text, and
  `showError(el, err)` puts it inline beside the control that failed. A `fetch`
  that never completed is reported as "could not reach Unwatcharr", not as a
  `TypeError`.
- **Every empty state renders through `_empty.html`**, so "there is nothing
  here" always arrives as the same shape: what is missing, why, and a numbered
  callout naming the screen and the control to use next. `blank.filtered()` is
  the quieter variant for "this filter matched nothing" — the data exists, the
  view narrowed past it, and no step list belongs there.
- The two compatibility blocks (the `theme.css` alias block and `app.css`
  section 14) are **deleted**. No `--accent` / `--muted` / `--radius` / `--mono`
  alias and no `.card` / `.banner` / `.badge` / `.stat` / `.row` / bare-element
  styling remains — a new page uses the component vocabulary or adds to it.

Design progress and the block order live in [HANDOVER.md](HANDOVER.md).

## Database rules

- **Migrations build the schema from zero.** A brand new file is at
  `user_version = 0` and gets the baseline by running migration 001, exactly as
  an existing database would. Fresh install and upgrade take the same code path,
  so the migration machinery is exercised on every install.
- Adding a schema change = append a `Migration` to `MIGRATIONS` in
  `app/migrations.py` with the next version number. `SCHEMA_VERSION` derives
  itself from that tuple. **Never edit a migration that has shipped.**
- `CREATE TABLE IF NOT EXISTS` will never add a column to an existing database.
  A schema change is a migration, never a tweak to the baseline.
- `db.open_readonly()` opens a file through `file:…?mode=ro` (verified to reject
  writes on Windows) for anything that must be inspected without a chance of
  modifying it.
- `session_secret` is generated per install and is never shared between
  installations or baked into the image.

## Configuration lives in two places

`app/config.py` reads only what must be known before the database is open:
`CONFIG_DIR`, `HOST`, `PORT`, `LOG_LEVEL`, `TZ`, plus first-boot seeds. Anything
a user can change — Plex connection, schedule, safe mode, notifications,
retention, UI password — lives in the `settings` table, declared once in
`store.CONFIG_DEFAULTS`.

Env pre-seeds apply on **first boot only**, so a stale compose file cannot
clobber what was configured in the UI.

### Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `CONFIG_DIR` | `./config` | Database at `$CONFIG_DIR/unwatcharr.db`, logs at `$CONFIG_DIR/logs` |
| `HOST` | `0.0.0.0` | |
| `PORT` | `8577` | Pinned to the published port mapping |
| `LOG_LEVEL` | `info` | `debug`\|`info`\|`warning`\|`error` |
| `TZ` | `UTC` | Resolved through `timeutil.resolve()`; an unknown zone logs a warning and falls back, never raises |
| `PLEX_URL` | — | First-boot seed |
| `PLEX_TOKEN` | — | First-boot seed. Consumed in `store.ensure_bootstrap()` |
| `PUID` / `PGID` | `1000` | Container only; entrypoint chowns `/config` and drops root |

Adding a setting = a default in `store.CONFIG_DEFAULTS` + validation in the
settings API. Secrets must also be listed in `store.SECRET_CONFIG_KEYS`, which
`public_config()` strips.

## Security invariants

- **No Plex token ever reaches the browser.** `store.public_config()` strips
  `SECRET_CONFIG_KEYS`; a test asserts no token appears in any response body.
- `logging_conf` redacts tokens at the handler: registered secrets are replaced
  verbatim, plus regex patterns for `X-Plex-Token=…` and `?token=…`. Call
  `store.register_known_secrets()` after loading tokens.
- Any server-side fetch of a stored URL needs an allowlist, or it becomes an
  open proxy into whatever the container can reach:
  - plex.tv avatars → `plex_account._host_allowed()` (https + plex.tv or a
    subdomain; rejects `plex.tv.evil.com`)
  - PMS artwork → `client.is_safe_artwork_path()` (`/library/` or `/photo/`,
    no scheme, no `//`, no `..`). Passing any path straight through would make
    `/:/unscrobble` was reachable through the thumb proxy.
- Database file is chmod 600 where the filesystem allows it.

## Behavioural defaults worth preserving

`safe_mode` starts **on** and forces every run — scheduled or manual — to a dry
run. An item with no `lastViewedAt` is always skipped rather than guessed at.
Exclude filters beat include filters. Show-level collections/labels/genres are
pushed down onto episodes before filtering. Undo re-scrobbles, which cannot
restore the original watch date or play count — say so rather than implying
otherwise.

## Traps already hit in this build

- **Long heredocs through the Bash tool get truncated** and produce
  `unexpected EOF`. Write files over ~150 lines with the Write tool.
- **A Jinja block may only be declared once per template.** `base.html` switches
  between the bare and the full shell by opening and closing the wrapper markup
  in matching `if` branches around a single `{% block content %}` — not by
  declaring the block in both branches, which raises at import.
- **`%-d` in `strftime` is glibc-only** and raises on Windows. The dev box is
  Windows and the container is Debian; use `%d`.
- **`{{ value | tojson }}` inside a double-quoted HTML attribute is broken.**
  `tojson` marks its output safe and emits raw `"`, so
  `onclick="fn({{ name | tojson }})"` ends the attribute early on any value
  containing a quote, and the handler silently never runs. Put the value in a
  `data-*` attribute (normal autoescaping) and read it off the element.
- `sqlite3.executescript()` commits any open transaction, so the
  `PRAGMA user_version=N` bump must be a separate statement after it, not part
  of the same script.
- **Absent is not "unticked".** `services/rules.save_overrides()` no-ops when
  `user_overrides` is missing or empty, because a single-user server never
  renders that table and reading absence as intent silently disabled rules for
  everyone. To clear an override, send it explicitly.
- `httpx.MockTransport` is the cheapest way to test the Plex client: swap
  `server._client` for one built on it, and assert on the recorded
  `X-Plex-Token` per call. That is how the per-account invariant gets proved
  without a server.
- On Windows the read-only URI form that works is
  `sqlite3.connect(Path(p).resolve().as_uri() + "?mode=ro", uri=True)` —
  verified to raise on write. Do not hand-build `file:/…` strings.

## Build status

See [HANDOVER.md](HANDOVER.md) for current phase, what just landed, and the exact
next step.
