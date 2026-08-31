# Unwatcharr 2.1

A sidecar container that marks old watched Plex media **unwatched** again, on a
schedule, from a web UI — so your library keeps resurfacing things you loved
instead of hiding them behind a grey checkmark forever.

Plex removed plugin support in 2018, so this cannot run inside Plex. It talks to
the Plex HTTP API from outside, on port `8577`.

```
docker compose up -d          # then open http://<host>:8577
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

## The one thing to understand about undo

Undo re-scrobbles the item. **Plex records a fresh play**, so the original watch
date and play count are gone for good. Undo restores "watched", not "watched on
14 March 2023, four times". Every surface in the app says so.

## Quick start

Save this as `docker-compose.yaml`:

```yaml
services:
  unwatcharr:
    image: ghcr.io/issaci22/unwatcharr:latest
    container_name: unwatcharr
    ports:
      - "8577:8577"
    volumes:
      # A host path or a named volume, as long as PUID:PGID can write to it.
      # On TrueNAS SCALE point this at a dataset, e.g.
      #   /mnt/tank/apps/unwatcharr:/config
      - ./config:/config
    environment:
      TZ: ${TZ:-America/New_York}
      # Must match the owner of the directory mounted at /config. `ls -n` on the
      # host shows the numeric ids; on TrueNAS SCALE `apps` is 568:568.
      PUID: ${PUID:-1000}
      PGID: ${PGID:-1000}
      # Pinned so it always pairs with the published 8577:8577 mapping --
      # change both together, or change neither.
      PORT: "8577"
      LOG_LEVEL: ${LOG_LEVEL:-info}
      # Optional pre-seeds, applied on FIRST BOOT ONLY -- after that the web UI
      # owns them and a stale compose file cannot clobber the browser. Leave
      # both unset and use the setup wizard's plex.tv link code instead.
      PLEX_URL: ${PLEX_URL:-}
      PLEX_TOKEN: ${PLEX_TOKEN:-}
    restart: unless-stopped
```

Set `TZ` and `PUID`/`PGID` to match the owner of the directory you mount at
`/config` (`ls -n` shows the numeric ids), either in the file or in a `.env`
beside it — [`.env.example`](.env.example) documents every variable. Then:

```bash
docker compose up -d
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
| [docs/API.md](docs/API.md) | The complete JSON API contract |
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
app/services/      the seam: setup, users, rules, runs, status
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
4. `session_secret` is generated per install and never baked into the image.
