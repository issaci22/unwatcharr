# Unwatcharr

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img src="docs/assets/logo-light.svg" width="72" height="72" alt="Unwatcharr">
  </picture>
</p>

<p align="center">
  <strong>Marks old watched Plex media unwatched again — on a schedule, per user, from a web UI.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-3b82f6?style=flat-square"></a>
  <a href="https://github.com/issaci22/unwatcharr/pkgs/container/unwatcharr"><img alt="Docker image" src="https://img.shields.io/badge/ghcr.io-issaci22%2Funwatcharr-2496ed?style=flat-square&logo=docker&logoColor=white"></a>
  <img alt="Container profile" src="https://img.shields.io/badge/Image-206%20MB-22c55e?style=flat-square">
  <img alt="Platform" src="https://img.shields.io/badge/Platform-linux%2Famd64%20%C2%B7%20arm64-475569?style=flat-square">
  <img alt="Engine" src="https://img.shields.io/badge/Python-3.12-3776ab?style=flat-square&logo=python&logoColor=white">
  <a href="https://github.com/issaci22/unwatcharr/actions/workflows/docker-publish.yml"><img alt="Build" src="https://img.shields.io/github/actions/workflow/status/issaci22/unwatcharr/docker-publish.yml?branch=main&style=flat-square&label=build"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#feature-matrix">Features</a> ·
  <a href="docs/INSTALL.md">Install</a> ·
  <a href="docs/CONFIGURATION.md">Configuration</a> ·
  <a href="docs/API.md">API</a>
</p>

---

Your library keeps resurfacing things you loved instead of hiding them behind a
grey checkmark forever. Plex removed plugin support in 2018, so Unwatcharr cannot
run *inside* Plex — it is a sidecar container that speaks to the Plex HTTP API
from outside, on port `8577`.

---

## Quick start

```yaml
services:
  unwatcharr:
    image: ghcr.io/issaci22/unwatcharr:latest
    container_name: unwatcharr
    environment:
      TZ: America/New_York
      PUID: 568
      PGID: 568
    volumes:
      - ./config:/config
    ports:
      - "8577:8577"
    restart: unless-stopped
```

```bash
docker compose up -d          # then open http://<host>:8577
```

That is the whole install. Every push to `main` publishes a multi-arch image
(amd64 + arm64) to GHCR, so nothing is built and no source checkout is required.

### Environment variables

| Variable | Default | Required | What it does |
|---|---|:---:|---|
| `TZ` | `UTC` | Recommended | Drives the scheduler and every timestamp in the UI. Resolved through `timeutil.resolve()`; an unknown zone logs a warning and falls back rather than raising. |
| `PUID` | `568` | Recommended | Numeric UID the app drops to. **Must own the directory mounted at `/config`** or the database cannot be written. `ls -n` on the host shows it; on TrueNAS SCALE `apps` is `568`. |
| `PGID` | `568` | Recommended | Numeric GID, paired with `PUID`. Same rule, same failure mode. |
| `PORT` | `8577` | No | The in-container listen port. **Pinned to the published mapping** — change `PORT` and `8577:8577` together, or change neither. |
| `LOG_LEVEL` | `info` | No | `debug` \| `info` \| `warning` \| `error`. |
| `CONFIG_DIR` | `/config` | No | Database at `$CONFIG_DIR/unwatcharr.db`, logs at `$CONFIG_DIR/logs`. |
| `PLEX_URL` | — | No | First-boot seed only. The setup wizard discovers this without you. |
| `PLEX_TOKEN` | — | No | First-boot seed only, consumed by `store.ensure_bootstrap()`. |

> **First-boot seeds, not live config.** `PLEX_URL` and `PLEX_TOKEN` apply on the
> very first boot and never again, so a stale compose file can never clobber what
> you configured in the browser. Everything a user can change lives in the
> settings table and survives a `docker compose up -d --force-recreate`.

### Port bindings

| Host | Container | Protocol | Purpose |
|---|---|---|---|
| `8577` | `8577` | TCP/HTTP | Web UI, the JSON API, and the unauthenticated `/healthz` probe. |

The image declares a `HEALTHCHECK` against `/healthz` every 60s, so `docker ps`
reports real health rather than "up".

<details>
<summary><strong>Full annotated compose file</strong> — every optional variable, mirrored from <code>docker-compose.yaml</code></summary>

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

[`.env.example`](.env.example) documents every variable for use in a `.env` file
beside the compose file.

</details>

Then open `http://<host>:8577`, run the setup wizard (sign in with a plex.tv link
code — no token hunting), pick your server, link your users, write a rule,
**preview it**, and turn safe mode off only once you believe the preview.

Full instructions, including TrueNAS SCALE and bare metal: **[docs/INSTALL.md](docs/INSTALL.md)**.

---

## Feature matrix

### **Automated Plex Sweep Engine**

A rule is *one media type* + *one or more libraries of that type* + a timer, and
the scheduler runs it without you. Evaluation is a pure function — no I/O, no
DB, no clock of its own — so every decision is reproducible and unit-tested.

| Capability | Detail |
|---|---|
| **Age threshold** | "Watched more than *N* days / weeks / months / years ago", resolved against an explicit `now`. |
| **Play-count floor** | `min_view_count` — leave anything watched fewer times than the floor alone. |
| **In-progress protection** | Items with a resume point are skipped, and so is anything **being played right now**. |
| **Series completion gate** | A show is only touched once every episode is watched, with `tv_scope` selecting episode-level or series-level collapse. |
| **Scheduling** | Interval or cron, with catch-up on boot for runs missed while the box was down. |
| **Notifications** | Webhook, Discord or ntfy — and **silent when a run did nothing**. |
| **Full history and undo** | Every applied change is recorded and reversible, individually or a whole run at a time. |

### **Advanced Multi-Library Policies**

| Capability | Detail |
|---|---|
| **Many libraries per rule** | `rule_libraries` is a join table, not a column. One policy spans every 4K, anime and kids library of the same type. |
| **Include / exclude filters** | Match on `collection`, `label`, `genre` or `title` substring. |
| **Exclude beats include** | Deliberately, so a "never touch this" filter can never be out-voted. |
| **Show tag inheritance** | Show-level collections, labels and genres are pushed down onto episodes *before* filtering, so a tag on the series protects the whole series. |
| **Filters are re-checked locally** | Server-side Plex filters are an optimisation only — Plex silently ignores a filter it does not recognise and returns the whole library, so every field is re-evaluated in `rules.py`. |

### **Granular Multi-User Exclusions**

Plex watch state is per-account, and a token only reads and writes the watch
state of the account it belongs to. Unwatcharr is built around that constraint
rather than papering over it.

| Capability | Detail |
|---|---|
| **Per-user evaluation** | The runner fans out to one pass **per rule per user**, re-fetching the library with each user's own token. Nobody's history bleeds into anyone else's. |
| **Opt a user out of a rule** | `rule_users.enabled = 0` excludes one user from one rule without touching the rule. |
| **Per-user thresholds** | A `NULL` override means "inherit the rule default", so raising the default later still moves everyone who never set their own. |
| **Owner / home / managed users** | Linked automatically — Unwatcharr mints a server-scoped token through plex.tv's home-switch endpoint. |
| **Shared users** | Plex offers no admin route to a friend's token, so it is pasted once. A user without a working token is simply left alone, never guessed at. |
| **Absent ≠ unticked** | Clearing an override requires sending it explicitly, so a single-user server that never renders the overrides table cannot silently disable rules for everyone. |

### **Safe Mode Dry-Runs**

| Capability | Detail |
|---|---|
| **On by default** | While safe mode is on, *every* run — scheduled or manual — is downgraded to a dry run. Nothing in Plex is modified. |
| **Its own endpoint** | Turning it off takes an explicit confirmation, never a checkbox buried in a long form. |
| **Loud on every page** | A full-width banner in one of two states that do not look alike, plus a chip in the sidebar and the app bar. |
| **Preview before you commit** | Preview one rule against one user, live. Previews write nothing, record no run and send no notification. |
| **Skip reasons, per item** | Eleven distinct reasons — *watched too recently*, *series is not fully watched*, *Plex has no watch date*, *matched an exclude filter* — so you know why each item was left alone. |
| **`changed_anything`** | A dry run that matched 400 items changed nothing, and every surface says so instead of blurring dry and apply. |

### **Zero-Leak Security Boundaries**

| Capability | Detail |
|---|---|
| **No token reaches the browser** | `store.public_config()` strips `SECRET_CONFIG_KEYS`, and a test asserts no Plex token appears in any response body. |
| **Redaction at the log handler** | Registered secrets are replaced verbatim, plus regex patterns for `X-Plex-Token=…` and `?token=…`. |
| **Host allowlist** | plex.tv avatars are fetched only over https from `plex.tv` or a subdomain — `plex.tv.evil.com` is rejected. |
| **Path allowlist** | The artwork proxy accepts `/library/` and `/photo/` only, with no scheme, no `//` and no `..`. Passing a path straight through would put `/:/unscrobble` behind the thumb proxy. |
| **Per-install session secret** | Generated at first boot and never baked into the image — the build runs its import check in a throwaway `CONFIG_DIR` and deletes it, precisely so every container does not ship the same secret. |
| **Least privilege at runtime** | The entrypoint chowns `/config` only when ownership is actually wrong, then `setpriv --reuid --regid --clear-groups`. It no-ops when already non-root. The database is `chmod 600` where the filesystem allows it. |

---

## The one thing to understand about undo

Undo re-scrobbles the item. **Plex records a fresh play**, so the original watch
date and play count are gone for good. Undo restores "watched" — not "watched on
14 March 2023, four times". Every surface in the app says so, and no surface
implies otherwise.

---

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

| Boundary | Rule it holds |
|---|---|
| `engine/rules.py` | Pure. Takes fetched items plus an explicit `now`, returns a `Decision` per item. No HTTP, no DB, no clock. |
| `plex/client.py` | Takes `token` as a **per-call argument**, never as client state — one run hits the same server with several users' tokens over one pool. |
| `store.build_rule()` | The single place a per-user override resolves into an effective threshold. The engine has no idea per-user rules exist. |
| `app/migrations.py` | Forward-only, baseline-as-migration-001. Fresh install and upgrade take the same code path. |
| Process model | **One uvicorn worker.** The scheduler, the run lock and the SQLite connection are process-local; a second worker double-runs every rule. |
| Dependencies | No `python-plexapi`, no CDN assets, no build step — the NAS may have no outbound internet. |

---

## API conventions

**The JSON API is the single contract, and it is frozen.** Roughly 40 endpoints
cover every read and every mutation; the server-rendered pages are a consumer of
the same viewmodels, never a second source of truth.

Frontend work starts and ends at **[docs/API.md](docs/API.md)** — every endpoint,
every viewmodel field, every documented JSON schema. Conventions that hold across
all of them:

- **Every response shape is a viewmodel.** `web/viewmodels.py` holds one set of
  dict builders shared by the JSON API and the templates. A new field goes into
  `viewmodels.py` and `docs/API.md` in the same commit — never straight into a
  template.
- **Errors carry a human sentence.** Every failure returns a `detail` string
  already written for a person. Clients render it verbatim; a status code is
  never shown as a number.
- **Reads are `GET`, mutations are `POST`.** Deliberate, irreversible actions
  (disabling safe mode, undo) have their own endpoints rather than a boolean on
  a larger payload.
- **`/healthz` is the only unauthenticated endpoint.**
- **Secrets are stripped server-side**, not hidden client-side.

When an endpoint, a viewmodel field or a `CONFIG_DEFAULTS` key changes,
`docs/API.md` and `docs/CONFIGURATION.md` change in the same commit.

---

## Documentation

| Document | What is in it |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Docker, TrueNAS SCALE, bare metal, PUID/PGID, first-run wizard, troubleshooting |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every environment variable and every setting |
| [docs/API.md](docs/API.md) | The complete, frozen JSON API contract |
| [CLAUDE.md](CLAUDE.md) | Engineering guide: invariants, architecture, traps already hit |

---

## Requirements

| | |
|---|---|
| **Runtime** | Docker — or Python 3.12 and `pip install -r requirements.txt` |
| **Plex** | A Plex Media Server you own, reachable from the container |
| **Memory** | ~256 MB |
| **Image** | 206 MB, single stage, `linux/amd64` + `linux/arm64` |

---

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/e2e   # 140 unit tests
.venv/Scripts/python.exe -m pytest tests/e2e -q                   #  50 e2e tests
```

The e2e suite boots the real app against a token-aware mock Plex server and
asserts, among other things, that each user's token only ever touches that user's
watch state and that no Plex token appears in any response body.

---

## License

[MIT](LICENSE).
