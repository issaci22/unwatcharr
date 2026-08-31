# Installing Unwatcharr

Unwatcharr runs as a container next to Plex. It needs one writable directory
(`/config`) and one port (`8577`).

---

## 1. Docker Compose (pre-built image, recommended)

Every push to `main` publishes a multi-arch image (amd64 + arm64) to GitHub
Container Registry, so there is nothing to build and no source checkout to keep.

```bash
mkdir unwatcharr && cd unwatcharr
curl -O https://raw.githubusercontent.com/issaci19/unwatcharr/main/docker-compose.prod.yml
curl -o .env https://raw.githubusercontent.com/issaci19/unwatcharr/main/.env.example
$EDITOR docker-compose.prod.yml   # set the image owner
$EDITOR .env                      # set TZ, PUID, PGID
docker compose -f docker-compose.prod.yml up -d
```

`docker-compose.prod.yml` has no `build:` block — it points straight at
`ghcr.io/issaci19/unwatcharr:latest`. Point it at your own owner if you
forked the repo (the GHCR path is always lowercase), or pin a release such
as `:2.1.0` if you would rather choose when to move.

Its environment block is `TZ`, `PUID`, `PGID`, `PORT` and `LOG_LEVEL`, plus the
optional `PLEX_URL`/`PLEX_TOKEN` first-boot seeds — nothing else is read from
the environment.

## 2. Docker Compose (build from source)

```bash
git clone <this repo> && cd Unwatcharr
cp .env.example .env
$EDITOR .env                  # set TZ, PUID, PGID
docker compose up -d --build
```

`docker-compose.yml` builds the image, bind-mounts `./config`, and publishes
`8577:8577`. Its environment block is `TZ`, `PUID`, `PGID`, `PORT` and
`LOG_LEVEL`, plus the optional `PLEX_URL`/`PLEX_TOKEN` first-boot seeds. `PORT`
is pinned to `8577` in the compose file so it always matches the published
mapping — if you move the app to another port, change the mapping and `PORT`
together.

## 3. Docker CLI

```bash
docker build -t unwatcharr:latest .
docker run -d \
  --name unwatcharr \
  -p 8577:8577 \
  -v /path/on/host/unwatcharr:/config \
  -e TZ=America/New_York \
  -e PUID=1000 -e PGID=1000 \
  --memory 256m \
  --restart unless-stopped \
  ghcr.io/issaci19/unwatcharr:latest
```

## 4. TrueNAS SCALE

Easiest path: use `docker-compose.prod.yml` and change the volume to your
dataset — SCALE pulls the published image and never needs a toolchain.

```bash
docker compose -f docker-compose.prod.yml up -d
```

`docker-compose.truenas.yml` is still there for an air-gapped NAS. It expects
`unwatcharr:latest` to already exist on the host — build it once over SSH, or
`docker load` an image exported elsewhere.

```bash
docker compose -f docker-compose.yml build
docker compose -f docker-compose.truenas.yml up -d
```

Two things to get right:

- **The dataset.** Point the volume at a real dataset, e.g.
  `/mnt/tank/apps/unwatcharr:/config`.
- **PUID/PGID.** On TrueNAS SCALE the `apps` user is usually **568:568**. Run
  `ls -n` on the parent directory to see the numeric owner and match it.

Getting PUID/PGID wrong is the single most common cause of a container that
will not start: the app cannot write its database and says so, in plain words,
in the log.

## 4. Without Docker

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
CONFIG_DIR=./config TZ=America/New_York .venv/bin/python -m app
```

Runs on `0.0.0.0:8577` with a single worker. **Do not raise the worker count** —
the run manager, its lock and the scheduler are all process-local, so a second
worker would run your rules twice.

---

## PUID / PGID and the config volume

The entrypoint starts as root, ensures `/config` exists, and chowns it to
`PUID:PGID` **only when the ownership is actually wrong** (recursively chowning
a large directory on every boot is wasted I/O on a NAS). It then drops
privileges with `setpriv --reuid --regid --clear-groups` and execs the app.

If the container is already started as a non-root user (compose `user:`), the
entrypoint skips all of that and execs immediately.

If the chown fails, you get a warning telling you to fix the host directory's
ownership yourself. The app will then fail to open its database, and the error
text names PUID/PGID explicitly.

## What lives in /config

```
/config/unwatcharr.db     SQLite database: settings, rules, users, runs, history
/config/logs/             only written when `log_to_file` is turned on
```

Back up `unwatcharr.db` and you have backed up everything. Deleting the volume
resets the app to a fresh setup wizard.

## Health check

```bash
curl http://<host>:8577/healthz
```

`/healthz` is the one unauthenticated endpoint. The image also declares a
`HEALTHCHECK` that hits it every 60s, so `docker ps` shows real health.

---

## First run

1. Open `http://<host>:8577`.
2. If a Plex-Unwatcher v1 database is detected inside `/config`, the wizard
   offers to import it. See [UPGRADING.md](UPGRADING.md). The v1 file is opened
   read-only.
3. **Connect to Plex.** The recommended path is the plex.tv link code: the
   wizard shows a short code, you approve it at `plex.tv/link`, and it lists the
   servers on your account. Pick one and it probes every address Plex advertises
   for it until one answers from inside the container.
   - If none are reachable (common when Plex uses host networking and the
     advertised addresses are container-internal), use the manual option and
     give it a LAN address such as `http://192.168.1.10:32400` plus a token.
4. **Link your users.** Owner, Plex Home and managed users can be linked
   automatically — Unwatcharr mints a server-scoped token for them. Shared users
   *cannot*: Plex offers no admin route to a shared user's token, so you must
   paste one. A user without a working token is simply left alone.
5. **Set a UI password.** Until you do, anyone who can reach port 8577 can change
   your Plex watch history. The dashboard nags you about this.
6. **Write a rule**, then **preview it** against a real user. Read the skip
   reasons. They tell you exactly why each item was left alone.
7. **Turn safe mode off** only once a preview showed you what you expected. It
   needs an explicit confirmation.

## Upgrading the container

```bash
docker compose -f docker-compose.prod.yml pull   # pre-built image
docker compose -f docker-compose.prod.yml up -d
```

Building from source instead:

```bash
docker compose up -d --build
```

The database migrates forward automatically on boot. `/config` is untouched, so
every setting, rule and history row survives a recreate. Environment variables
like `PLEX_URL`/`PLEX_TOKEN` are **first-boot seeds only** — a stale compose file
can never clobber what you configured in the browser.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Container exits at boot, log mentions PUID/PGID | `/config` is not writable by `PUID:PGID`. Fix ownership on the host. |
| "None of the addresses Plex lists ... could be reached" | Plex's advertised addresses are not routable from the container. Use the manual setup option with a LAN IP. |
| A user is never touched by any run | They have no working token. Check the Users page for `token_status`. |
| A run says it applied nothing | Safe mode is on (by design), or no rule matched. Check the run detail's skip reasons. |
| Schedule change did not take effect | It should — every settings save reschedules. If not, check `schedule_enabled` and the Status panel's `next_run_at`. |
| Timestamps are wrong | `TZ` is unset or unknown. An unknown zone logs a warning and falls back to the system default. |
