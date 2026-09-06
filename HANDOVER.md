# HANDOVER

Current state and the next step. **[CLAUDE.md](CLAUDE.md) is the engineering
guide** — architecture, Docker internals, security invariants, database rules
and the trap list live there and are deliberately not repeated here.

```yaml
project: Unwatcharr
version: 1.1.1                     # 1.1.X series -- do not renumber
workspace: D:\Documents\Claude Projects\Unwatcharr

milestone:
  backend: FEATURE-COMPLETE AND FROZEN
  design_phase: COMPLETE -- all 10 blocks (shell, dashboard, rules+editor,
                preview, users, history, logs, settings, setup wizard,
                login/empty/error states)
  packaging: repo live, GHCR pipeline live, docker-compose.yaml (GHCR pull)
             + docker-compose.dev.yaml (builds the working tree, local only)
  outstanding: nothing in the build. Open items are operational -- see
               "Open work" below.

tests: 199/199 green
  unit: 123    .venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/e2e
  e2e:   76    PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/e2e -q

git:
  branch: main, pushed to origin/main
  head: aaa5dcd chore(docker): local-only dev compose stack
                (docker-compose.dev.yaml · CLAUDE.md · HANDOVER.md)
  recent: f8048de fix(web): cache-bust /static so an upgrade cannot serve
                  stale CSS and JS  (app/main.py · app/web/pages.py ·
                  base.html · tests/e2e/test_e2e.py · CLAUDE.md trap entry)
  note: both were rebased onto three README-only commits that landed on
        origin/main mid-session (7202ebb, 94acedd, a9fca1f), so their
        pre-rebase hashes are dead. Trust `git log`, not a remembered hash.
  working_tree: CLEAN
  version: 1.1.1, released as the annotated tag v1.1.1. The 2.1.x numbering
           was abandoned -- a stray local `2.1.1` tag and old 2.1.x GHCR tags
           survive from it and were deliberately LEFT ALONE, not cleaned up.
  identity: repo-local IssacPC <issacthrowaway69@gmail.com>
  caution: something outside the session has overwritten files in this
           workspace before. Re-read from disk before trusting stale state.

docker:
  prod_image: ghcr.io/issaci22/unwatcharr:latest (206 MB, amd64 + arm64)
  publishes_on: push to main, tags v*.*.*, workflow_dispatch
  auth: GITHUB_TOKEN only -- NO SECRETS TO CONFIGURE
  running_container: BEHIND the tree. It predates the UI fixes in 0e8060e.
                     It is NOT on this box -- `docker ps -a` here is empty, so
                     8577 is free locally and both stacks can coexist.
  gh_cli: NOT INSTALLED -- no repo or GHCR package administration from this box.
  dev_image: unwatcharr:local (218 MB, built from the working tree, never pushed)
    file: docker-compose.dev.yaml   project: unwatcharr-dev
    container: unwatcharr-dev       url: http://localhost:8578
    up:   docker compose -f docker-compose.dev.yaml up -d --build --wait
    down: docker compose -f docker-compose.dev.yaml down      # -v also wipes /config
    why:  test the EXACT working tree, uncommitted changes included, without
          pushing to GHCR first. Verified 2026-09-06 -- see "Verified" below.

env:
  python: 3.12.10 · fastapi 0.141.1 · starlette 1.6.0 · docker 29.7.2
  venv: .venv (deps installed)
```

## Constraints a new session will otherwise break

- **The JSON API is the single contract and it is FROZEN.** Do not edit without
  explicit authorization:
  `app/web/api.py` · `app/web/viewmodels.py` · `app/services/` · `app/engine/` ·
  `app/plex/` · `app/store.py` · `app/db.py` · `app/migrations.py`
- **In scope:** `app/web/templates/*.html` · `app/web/static/*` ·
  `app/web/pages.py` (HTML delivery parameters only).
- A new field goes into `viewmodels.py` **and** `docs/API.md` in the same
  commit — never straight into a template.
- **Two compose files, doing opposite things.** `docker-compose.yaml` pulls the
  GHCR image and **never builds** — README.md and docs/INSTALL.md paste its
  body verbatim, so **edit all three together**; it is the one place the docs
  can silently drift. `docker-compose.dev.yaml` is the opposite and is for
  local testing only: it builds the working tree, names no registry, and is
  deliberately absent from README/INSTALL. Do not merge them, and do not
  "tidy up" by deleting the dev file.
- Never `docker push` by hand; the workflow is the only thing that publishes.
- One uvicorn worker only. No CDN assets, no build step, no framework.
- Vanilla JS + server-rendered Jinja. Framework-shaped fixes (React/Vue) do not
  apply to this codebase.

## Recent changes

- **`f8048de` static asset cache busting.** `/static` was served with an
  ETag but no `Cache-Control`, so browsers applied heuristic freshness and kept
  serving the *previous* release's `app.css`/`app.js` for hours after an
  upgrade. Two UI bugs reported as regressions (empty red error bar in the rule
  editor, dead sidebar hamburger) were already fixed in `0e8060e` and were only
  still on screen because of this. Fix: `_asset_version()` in `pages.py` exposes
  an `asset_v` Jinja global; `base.html` links all three assets as
  `?v={{ asset_v }}`; `VersionedStatic` in `main.py` answers a stamped URL
  `immutable` and a bare one `no-cache`. +7 e2e tests (69 → 76). No change was
  needed in `app.css` or `app.js`.
- **`docker-compose.dev.yaml` (new file, committed).** A local build/test
  stack so a change can be exercised in a real container before it is committed
  or published. Builds the working tree into `unwatcharr:local`; names no
  registry, so it cannot pull or push. Separate compose project, container
  (`unwatcharr-dev`), host port (8578) and named volume, so it never collides
  with — or writes to — anything the production stack owns.
  `docker-compose.yaml` was **not touched**; CLAUDE.md and HANDOVER.md were,
  because both asserted "one compose file" as an invariant.
- `59a92c7` version set to 1.1.0.
- `0e8060e` three UI fixes: the rule editor's empty error bar, removal of the
  redundant full-width mode banner (the sidebar chip carries mode now), and the
  dead sidebar toggle rebuilt as one hamburger — drawer close under 960px,
  64px icon rail above it.

## Open work

**Do first**

1. Redeploy so the running container stops serving pre-fix assets:
   `docker compose pull && docker compose up -d`
   (Ctrl+Shift+R is the interim workaround on the running one.) The image is
   rebuilt by the Actions workflow from the pushed commit — never by hand.
2. ~~Make the GHCR package public.~~ **DONE** — verified 2026-09-06: an
   anonymous token lists tags at `ghcr.io/v2/issaci22/unwatcharr/tags/list`
   (HTTP 200), so `docker compose up -d` works for everyone, not just the
   owner.

**Decision needed**

- `docs/PROJECT-BRIEF.md` still describes the v1 import feature, which was
  removed entirely. It is the original requirements document ("never delete"),
  so rewriting it would falsify the record — but it is PUBLIC in the repo.
  Decide whether the brief belongs in a public release at all.

**Known gaps**

- `collapse_to_series` (`tv_scope=series`) is unit-tested, no e2e path.
- Scheduler catch-up-on-boot is implemented but untested.
- Docker runtime is now **mostly** closed by `docker-compose.dev.yaml` (see
  "Verified"). Still not exercised: `/config` ownership against a **real bind
  mount** with PUID/PGID — the dev stack uses a named volume precisely because
  Docker Desktop's bind-mount layer does not honour the entrypoint's chown, so
  only a Linux host or the NAS can test that path; a run against a host-run mock
  Plex; and the **published GHCR image** itself, which is a different artifact
  from `unwatcharr:local`. Worth doing once before the next release tag.
- No CI tests, no linter, no formatter configured.

## Project map

Architecture and the module-by-module breakdown are in CLAUDE.md. Orientation
only:

```
app/config.py db.py store.py migrations.py   pre-DB env, ALL SQL, schema
app/plex/       client · account · types     token is a PER-CALL argument
app/engine/     rules(PURE) collect preview runner scheduler
app/services/   setup users rules runs status
app/web/api.py  viewmodels.py                THE CONTRACT -- frozen
app/web/pages.py                             page routes, display filters,
                                             _asset_version() -> `asset_v`
app/main.py                                  lifespan, SessionMiddleware,
                                             VersionedStatic (/static caching)
app/web/templates/  base.html (shell) _icons.html _empty.html + 8 pages
app/web/static/     theme.css (tokens) app.css app.js favicon.svg
                    linked as ?v={{ asset_v }} -- load-bearing, not decoration
tests/              123 unit · tests/e2e 76 (real subprocess app + mock PMS)
docs/               INSTALL CONFIGURATION API PROJECT-BRIEF
.github/workflows/docker-publish.yml         amd64+arm64 -> GHCR
docker-compose.yaml                          PROD: pulls GHCR, never builds
docker-compose.dev.yaml                      LOCAL TEST: builds the tree, :8578
```

## Verified

**199/199 green (123 unit + 76 e2e).** Covers: migrations from `user_version=0`;
token redaction and no-token-in-any-response; the per-account invariant (each
token touches only its own watch state); safe mode downgrading apply→dry with
zero Plex writes; preview writing nothing; undo; the artwork/plex.tv allowlists;
cross-origin POST rejected; every page rendering; and the shell markup checks
(mode chip present in both modes, no full-width banner, labelled hamburger,
rule-editor error callout hidden at rest, every static link cache-busted).

Browser-driven against the real app + mock Plex: the rule editor's error callout
is `display: none` at rest and carries its sentence only after a failed save;
the sidebar toggles 226px ↔ 64px repeatedly and across a reload; zero console or
`pageerror` output; a stamped asset URL answers `immutable`, a bare one
`no-cache`, and `If-None-Match` returns 304 with an empty body.

**Local dev Docker stack, verified end to end 2026-09-06** (`unwatcharr:local`,
container `unwatcharr-dev`, http://localhost:8578):

- Container reaches `healthy`; `/healthz` 200, `/` 200, entrypoint drops to
  uid=gid=1000 and the app logs `Unwatcharr 1.1.1 starting`.
- **It is the working tree, not a release.** `sha256sum` inside the container
  matches the host byte-for-byte for `app/main.py`, `app/web/pages.py` and
  `base.html` — all three UNCOMMITTED. The proof that needs no hashing: the
  container answers `/static/app.css` with `Cache-Control: no-cache` and
  `?v=...` with `immutable`, which is `VersionedStatic` — code that exists
  only in the dirty tree, not in `59a92c7` and not in the GHCR image.
- **No stale layers.** A marker appended to `base.html`, rebuilt, was served
  over HTTP; removed, rebuilt, gone. `pull_policy: build` reproduced this on a
  bare `up -d` with no `--build` flag.
- **Nothing pulled or pushed.** `docker images` lists exactly one image,
  `unwatcharr:local`. No `ghcr.io/*` image exists on this box at any point.
- **Production untouched.** `git diff docker-compose.yaml` is empty; it still
  resolves to `image: ghcr.io/issaci22/unwatcharr:latest` with zero `build:`
  keys, under its own project name `unwatcharr`.
- **Isolation.** Dev DB lives in the named volume
  `unwatcharr-dev_config` (—> `/var/lib/docker/volumes/...`), a different
  sha256 from the repo's `./config/unwatcharr.db`, whose mtime never moved.
- **Persistence.** A probe row written into the dev DB survived
  `down` → `up -d`; `down -v` is the way to reset to first boot.
- 199/199 tests still green after the change (123 unit + 76 e2e).

## Next action

Both commits are on `origin/main`, so the Actions workflow has rebuilt
`ghcr.io/issaci22/unwatcharr:latest` from source. Pull it onto the NAS
(`docker compose pull && docker compose up -d`) and confirm the "New rule"
dialog opens with no red bar and the sidebar hamburger collapses the rail —
this time without a hard refresh, which is the whole point of the fix.

From then on a change is testable **before** it is pushed:
`docker compose -f docker-compose.dev.yaml up -d --build --wait`, then
http://localhost:8578. The build has no outstanding work — the remaining items
are the GHCR package visibility and the PROJECT-BRIEF decision.
