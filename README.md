# Unwatcharr 2.1

A sidecar container that marks old watched Plex media **unwatched** again, on a
schedule, from a web UI — so your library keeps resurfacing things you loved
instead of hiding them behind a grey checkmark forever.

Plex removed plugin support in 2018, so this cannot run inside Plex. It talks to
the Plex HTTP API from outside, on port `8577`.

```
docker compose -f docker-compose.prod.yml up -d   # then open http://<host>:8577
```

---

## What it does

- **Rules.** A rule is *one media type* (movie or show) + *one or more libraries
  of that type* + a timer ("watched more than 90 days ago") + optional gates and
  filters. Rules run per user.
- **Per-user.** Every Plex user with a linked token is evaluated separately
  against their own watch state. Nobody's history bleeds into anyone else's.
- **Safe mode, on by default.** While safe mode is on, *every run is downgraded
  to a dry run* and nothing in Plex is ever modified. Turning it off requires an
  explicit confirmation, not a checkbox in a long form.
- **Preview before you commit.** Preview one rule against one user, live, and see
  exactly which items would change and why each one was skipped. Previews write
  nothing, record no run, and send no notification.
- **Full history and undo.** Every applied change is recorded and can be undone —
  individually or a whole run at a time.
- **Notifications.** Webhook, Discord or ntfy, silent when a run did nothing.
- **Scheduler.** Interval or cron, with catch-up on boot for missed runs.
- **v1 import.** Guided import from a Plex-Unwatcher v1 database. The old file is
  opened read-only and never modified.

## The one thing to understand about undo

Undo re-scrobbles the item. **Plex records a fresh play**, so the original watch
date and play count are gone for good. Undo restores "watched", not "watched on
14 March 2023, four times". Every surface in the app says so.

## Quick start

Pre-built image (no checkout, no build):

```bash
curl -O https://raw.githubusercontent.com/issaci19/unwatcharr/main/docker-compose.prod.yml
$EDITOR docker-compose.prod.yml   # set the image owner, TZ, PUID, PGID
docker compose -f docker-compose.prod.yml up -d
```

Or from source:

```bash
git clone <this repo> && cd Unwatcharr
cp .env.example .env        # edit TZ, PUID, PGID
docker compose up -d --build
```

Open `http://<host>:8577`, run the setup wizard (sign in with a plex.tv link
code — no token hunting), pick your server, link your users, write a rule,
**preview it**, then turn safe mode off when you believe the preview.

Full instructions: [docs/INSTALL.md](docs/INSTALL.md).

## Documentation

| Document | What is in it |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Docker, TrueNAS SCALE, PUID/PGID, first-run wizard |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every environment variable and every setting |
| [docs/UPGRADING.md](docs/UPGRADING.md) | Importing a Plex-Unwatcher v1 database |
| [docs/API.md](docs/API.md) | The complete JSON API contract |
| [docs/PROJECT-BRIEF.md](docs/PROJECT-BRIEF.md) | The original product brief |
| [CLAUDE.md](CLAUDE.md) | Engineering guide / architecture |

## Architecture in one screen

```
app/config.py      env-only settings (CONFIG_DIR, PORT, TZ) -- everything else
                   lives in the settings table so it survives a recreate
app/db.py          one shared SQLite connection, WAL
app/migrations.py  forward-only, baseline-as-migration-001
app/store.py       ALL SQL lives here. Nothing else writes a query.
app/plex/          typed, tolerant Plex client. Token passed per call.
app/engine/rules.py    PURE evaluation. No I/O, no DB, fully unit tested.
app/engine/collect.py  paged library fetch
app/engine/preview.py  ephemeral evaluation
app/engine/runner.py   RunManager singleton, undo
app/engine/scheduler.py APScheduler; the only caller of history pruning
app/services/      the seam: setup, users, rules, runs, status, migrate_v1
app/web/api.py     THE CONTRACT (~45 JSON endpoints)
app/web/pages.py   a deliberately disposable, unstyled UI over that API
```

The JSON API is the product's contract. The bundled HTML is plain, unstyled and
disposable — it exists so the app is usable, not so it is pretty. A designed
frontend is a separate phase and consumes [docs/API.md](docs/API.md).

## Requirements

- Docker (or Python 3.12 and `pip install -r requirements.txt`)
- A Plex Media Server you own
- ~256 MB RAM. The image is ~206 MB.

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/e2e   # 140 unit tests
.venv/Scripts/python.exe -m pytest tests/e2e -q                    # 50 e2e tests
```

The e2e suite boots the real app against a token-aware mock Plex server and
asserts, among other things, that each user's token only ever touches that
user's watch state and that no Plex token appears in any response body.

## Safety properties this project holds itself to

1. Safe mode is on by default and forces `apply` → `dry`.
2. No Plex token is ever emitted by the API or rendered into a page.
3. The artwork proxy has host and path allowlists — it is not an open proxy.
4. A v1 database is only ever opened read-only.
5. `session_secret` is never imported from v1 and never baked into the image.
