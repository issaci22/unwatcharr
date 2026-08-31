# HANDOVER

MACHINE-READABLE BUILD STATE. Overwritten after every substantial component.

```yaml
project: Unwatcharr 2.1
workspace: D:\Documents\Claude Projects\Unwatcharr
reference_readonly: D:\Documents\Claude Projects\Plex-Unwatcher
plan: C:\Users\IssacPC\.claude\plans\use-the-claude-md-file-wiggly-quiche.md
version: 2.1.0
updated: 2026-08-30 (v1 IMPORT FEATURE REMOVED ENTIRELY -- see `v1_removal`
         below; 123/123 unit, 50/50 e2e green after the removal;
         DESIGN COMPLETE 10/10; repo initialised, GHCR pipeline live, compose
         collapsed to a single docker-compose.yaml)

NOTE: this file was restored from an older on-disk copy at least twice during
the packaging session — a rewrite covering the repo/CI work was silently
reverted before it could be committed. If the state below contradicts the tree,
the tree wins.

milestone:
  phases: [A_foundation, B_plex, C_engine, D_services, E_web, F_docker, G_tests, H_docs]
  complete: [A_foundation, B_plex, C_engine, D_services, E_web, F_docker, G_tests, H_docs]
  current: BACKEND FROZEN. /design phase COMPLETE. Repo + CI packaging done.
  next: nothing outstanding in the build. Open items are operational, listed
        under `manual_steps_still_required`.
  ui_policy: SUPERSEDED. The /design phase is running now, so the UI is no longer
             disposable. The JSON API is still the single contract and is frozen.

design:
  frozen_do_not_touch:
    - app/web/api.py          # the JSON contract
    - app/web/viewmodels.py
    - app/services/ app/engine/ app/plex/ app/store.py app/db.py app/migrations.py
  in_scope:
    - app/web/templates/*.html
    - app/web/static/*
    - app/web/pages.py        # HTML delivery parameters only
  blocks:
    1_tokens_and_shell: COMPLETE
    2_dashboard: COMPLETE
    3_rules_list_and_editor: COMPLETE
    4_preview: COMPLETE
    5_users_and_user_detail: COMPLETE
    6_history_runs_actions_undo: COMPLETE
    7_logs: COMPLETE
    8_settings: COMPLETE
    9_setup_wizard: COMPLETE
    10_login_empty_and_error_states: COMPLETE
  verified_after_block_5: 140/140 unit, 50/50 e2e, 105 template/markup/API checks
  verified_after_block_8: 140/140 unit, 50/50 e2e, 171 template/markup/leak checks
  seed_harness: session scratchpad only, never kept in the repo. The block-4
                harness seeds a throwaway db (2 users incl. a hostile name, 2
                libraries, 1 rule), signs in with a real scrypt hash and asserts
                every selector the preview script reaches for exists.
repo:
  branch: main -> origin/main (pushed)
  remote: https://github.com/issaci22/unwatcharr.git
          (renamed from issaci19 on 2026-08-30; the old path 301-redirects)
  commits: e222b9f initial release · f0eedcb GHCR pipeline · 1872cfb single
           docker-compose.yaml
  gitignore_verified: .venv/ .env config/ __pycache__/ .pytest_cache/ *.db*
  identity: repo-local IssacPC <issacthrowaway69@gmail.com> (no global identity)
  gh_cli: NOT INSTALLED. No repo or package administration from this machine.
  incidents: HEAD was found detached at f0eedcb with the `main` ref missing
             (recreated with `git checkout -B main`); HANDOVER.md was reverted
             to an older copy on disk more than once. Something outside this
             session writes to the workspace — re-read before trusting state.

packaging:
  workflow: .github/workflows/docker-publish.yml
    triggers: push to main, tags v*.*.*, workflow_dispatch (no path filter --
              a tag push carries no diff, so paths-ignore can skip a release)
    auth: GITHUB_TOKEN + packages:write. NO SECRETS TO CONFIGURE.
    tags: latest, branch, {{version}}, {{major}}.{{minor}}, {{major}}, sha-short
    platforms: linux/amd64, linux/arm64 (QEMU; all deps are pure-python or
               manylinux wheels, so nothing compiles under emulation)
    cache: type=gha mode=max · smoke test runs the pushed DIGEST against /healthz
  docs_embed_compose: README.md and docs/INSTALL.md paste docker-compose.yaml's
                      body verbatim in a ```yaml block. EDIT ALL THREE TOGETHER
                      -- this is the one place the docs can drift from the tree.
  env_surface: TZ, PUID, PGID, PORT="8577", LOG_LEVEL + first-boot-only
               PLEX_URL / PLEX_TOKEN. No legacy v1 migration variables exist.

v1_removal:
  why: first public release -- there is no v1 to upgrade from in public.
  deleted: app/services/migrate_v1.py · tests/test_migrate_v1.py ·
           docs/UPGRADING.md
  code_stripped:
    app/config.py       V1_DB_CANDIDATES gone
    app/web/api.py      GET+POST /api/setup/v1-import gone (contract change)
    app/web/pages.py    setup page no longer resolves v1/target_empty
    app/services/status.py + app/store.py  migrated_from_v1[_at] gone from
                        CONFIG_DEFAULTS and the status payload
    templates/setup.html  step-1 offer card + its script (~7KB) gone
    templates/settings.html  "Imported from v1" row gone
    static/app.css      .v1card block gone
  kept: db.open_readonly() -- generic now, still covered by
        tests/test_store.py::test_read_only_open_refuses_writes
  NOT touched: docs/PROJECT-BRIEF.md still describes the v1 import. It is the
               original requirements document ("never delete", CLAUDE.md), so
               rewriting it would falsify the record. It is PUBLIC in the repo
               -- decide whether the brief belongs in a public release at all.
  test_count: 140 -> 123 unit (17 were migrate_v1 tests). 50/50 e2e unchanged.

manual_steps_still_required:
  - Make the GHCR package public (github.com/users/issaci22/packages) after the
    first workflow run, or the documented `docker compose up -d` fails for
    everyone but the owner.
  - Add a LICENSE file: the workflow stamps image.licenses=MIT, none exists.

env:
  venv: .venv (deps installed)
  python: 3.12.10 · fastapi 0.141.1 · starlette 1.6.0
  docker: 29.7.2 · image unwatcharr:latest built, 206MB
  run_scripts_with: PYTHONPATH=. .venv/Scripts/python.exe <script>
  run_unit: .venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/e2e
  run_e2e: PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/e2e -q
```

## ARCHITECTURE & VISUAL LAYER STATE (BLOCKS 1-8 COMPLETE)

*   **Design Tokens & Shell (Block 1):** theme.css (Dark default, designed light), app.css grid system, brand mark, responsive drawer (<=960px), status rail & real-time run indicator.
*   **Dashboard (Block 2):** 5 core operational questions answered, failed runs spotlighted, interactive run console, non-destructive confirmDialog modal.
*   **Rules Engine UI (Block 3):** Server-rendered policy cards cloned from <template>, inline form validation, raw tojson escaping vulnerabilities closed.
*   **Trust Preview (Block 4):** Ephemeral non-mutating preview ledger displaying matched vs. aggregated skip reason counts straight from schema vocabulary. 
*   **Users Management (Block 5):** 4 account semantic styles, URL-encoded avatar proxy piping, manual token entry fallbacks, decoupled per-user override PATCH matrix.
*   **History & Logging (Blocks 6-8):** Distinct Run vs. Action splits, descriptive irreversible Undo confirmations, monospace level-pill logging with background tab polling pauses, 6-pane central settings hub with localStorage state persistence.

All UI features are built using semantic HTML5 data slots, custom variables, and vanilla JS. Zero external CDNs or heavy frameworks.


### Next block needs approval

Block 9 is the setup wizard, then block 10 is login / empty / error states —
after which the two compatibility blocks (`theme.css`'s alias block and
`app.css` section 14) can be deleted, because no pre-design template will
remain.

## Files on disk

```
app/__init__.py          2.1.0
app/config.py            env-only; CONFIG_DIR, PORT 8577, TZ, PLEX_URL/PLEX_TOKEN, V1_DB_PATH
app/timeutil.py          resolve/local_tz/now/to_local/format_ts/iso/format_duration/relative
app/logging_conf.py      ring buffer 2000, RedactingFormatter, register_secret, file log
app/db.py                shared conn, WAL fallback, PUID/PGID error text, open_readonly()
app/migrations.py        baseline-as-migration-001, forward-only, SCHEMA_VERSION=1
app/store.py             ALL SQL. CONFIG_DEFAULTS, SECRET_CONFIG_KEYS, build_rule, set_run_mode
app/notify.py            webhook|discord|ntfy, silent-on-empty, never fails a run
app/main.py              lifespan, SessionMiddleware, /healthz, JSON error handlers
app/__main__.py          uvicorn workers=1
app/plex/types.py        MediaItem/Library/PlexAccount/PlexResource, tolerant parsers
app/plex/client.py       PlexServer (token per call), is_safe_artwork_path(), first_reachable
app/plex/account.py      PIN flow, resources, server_token, home_users, shared_users,
                         switch_home_user, fetch_avatar (+_host_allowed)
app/engine/rules.py      PURE. cutoff_timestamp, evaluate, summarise_skips,
                         inherit_show_tags, build_series_index, collapse_to_series
app/engine/collect.py    prefilter() + collect(): movies paged; TV shows-first then allLeaves
app/engine/preview.py    preview_rule() -> PreviewResult (EPHEMERAL)
app/engine/runner.py     RunManager singleton, PassResult/RunResult, undo_action, undo_run
app/engine/scheduler.py  APScheduler, reschedule(), catch-up-on-boot, only caller of prune
app/services/setup.py    connect / refresh_libraries / test_connection / section_tag_values
app/services/users.py    refresh_users (3 sources) / link_home_user / set_user_token / ...
app/services/rules.py    RuleError, validate, CRUD, save_overrides, effective_thresholds
app/services/runs.py     RunError, start/cancel/current/preview, history, undo, UNDO_CAVEAT
app/services/status.py   status() -- the one dashboard/health payload
app/web/security.py      scrypt, session+password stamp, rate limit, origin check
app/web/viewmodels.py    serialisers -- NEVER emit a token
app/web/api.py           the JSON contract, ~45 endpoints
app/web/pages.py         page routes + login/logout; shell status context,
                         `when` / `until` / `threshold` display filters,
                         _last_pass_by_rule, _overrides_by_user,
                         _policy_counts, _user_activity
app/web/templates/*.html base (REBUILT: app shell) + _icons.html macros;
                         dashboard (block 2), rules + preview (blocks 3-4);
                         users matrix + detail (block 5); login setup history
                         logs settings are still pre-design markup — blocks 6-10
app/web/static/          theme.css (tokens), app.css (shell + components),
                         app.js, favicon.svg. No htmx, no CDN, no build step.
tests/support.py         shared helpers (see NOTE below)
tests/conftest.py        unit-suite fixtures
tests/test_rules.py      gate matrix, filters, tag inheritance, series gate, collapse
tests/test_cutoff.py     DST-exact units, calendar months w/ clamping, 0=everything
tests/test_store.py      build_rule overrides, cascade, public_config, redaction
tests/test_plex_client.py MockTransport: paging, both allowlists, per-call tokens
tests/fixtures/*.json    3 recorded Plex payloads
tests/e2e/mock_plex.py   TOKEN-AWARE mock PMS, /__calls /__state /__reset
tests/e2e/conftest.py    session mock+app subprocess, per-test reset, wait_for_run
tests/e2e/test_e2e.py    50 tests
Dockerfile               single-stage, setpriv check, import check, HEALTHCHECK
docker-entrypoint.sh     PUID/PGID, conditional chown, setpriv drop
docker-compose.yaml      THE ONLY COMPOSE FILE: pulls ghcr.io/issaci22/
                         unwatcharr:latest, no build block. The prod/dev/truenas
                         variants were deleted (history: f0eedcb).
.github/workflows/docker-publish.yml  amd64+arm64 -> GHCR on push to main + v tags
.env.example             every env var, commented
README.md                front door
docs/INSTALL.md          docker/truenas/bare metal, first run, troubleshooting
docs/CONFIGURATION.md    every env var + every setting + rule fields + skip reasons
docs/API.md              THE DESIGN HANDOFF: every endpoint + every viewmodel field
docs/PROJECT-BRIEF.md    original brief, preserved verbatim
CLAUDE.md HANDOVER.md requirements*.txt pytest.ini .gitignore .dockerignore
```

## Verified

**A** migration 001 builds 9 tables from `user_version=0`, re-apply no-op ·
safe_mode defaults True · public_config strips secrets · redaction · rule↔library
cascade · read-only SQLite URI rejects writes on Windows.

**B** URL normalisation · artwork allowlist (2 allow / 6 block incl.
`/library/../:/unscrobble`) · plex.tv host allowlist rejects `plex.tv.evil.com` ·
401 on bad token · music unsupported · paging stable at page_size=2 · tolerant
parse · per-call tokens on writes.

**C** (19 checks) gate matrix · cutoff DST-exact + 31 Mar−1mo=28 Feb · exclude
beats include · tag inheritance · tv_scope=series collapses only whole shows ·
safe mode 0 writes · **each token touched only its own state** · per-user
override and opt-out · undo · preview ephemeral · notify silent-on-empty.

**D** (19 checks, real v1 data) detect BlockBuster 2.0 (2 rules/1 user/2 libs/2
runs) · import maps rules→media_type+rule_libraries, 2 v1 runs sharing batch
`f4912b4f2191` → 1 run + 2 passes (scanned 234) · **session_secret regenerated,
not imported** · **v1 source byte-for-byte untouched** · re-import refused ·
8 bad rule shapes rejected · **v1 trap held: absent/empty user_overrides never
disables a rule** · effective_thresholds inheritance · status() leaks no secret.

**E** **50/50 e2e passing (51s)** — setup discovers libraries (music excluded) ·
6 pages render · /healthz unauthenticated · multi-library rules · wrong-type
library rejected · movie rules normalise TV gates · editor creates nothing ·
edit does not clone · **safe mode downgrades apply→dry, 0 Plex writes, run row
records mode=dry** · safe-mode-off needs `confirm` (409) · dry run changes
nothing · apply unwatches exactly the right items (per-token state proves it) ·
second run no-op · clear_progress off by default · single-rule targeting · run
detail exposes passes + skip reasons · TV series gate protects partly-watched
show · **preview writes nothing, records no run, explains skips** · dry-run
candidates excluded from history · undo run + single undo · **no Plex token in
any API response or page** · thumb proxy refuses 5 hostile paths · 6 error cases
return flat `detail` · cross-origin POST 403 · settings round-trip + reschedule ·
settings hide secrets · bad enums rejected · integers clamped.

**F** **`docker build` clean, `unwatcharr:latest` = 206 MB** (target met).
Build-time `setpriv` check passes · import check under `CONFIG_DIR=/tmp/importcheck`
prints `import check OK` and the throwaway dir is deleted, so **no session secret
is baked into the image** · HEALTHCHECK on /healthz · `VOLUME /config`,
`EXPOSE 8577` · entrypoint chowns only when ownership is wrong, then
`setpriv --reuid --regid --clear-groups`.

**G** **123/123 unit tests green** (140 before the v1 removal). Ported from the phase B/C/D scratchpad smoke
scripts across `test_rules.py`, `test_cutoff.py`, `test_store.py`,
`test_plex_client.py`.

**H** README + 4 docs written from the code, not from memory: every endpoint in
`docs/API.md` was read out of `app/web/api.py`, every field out of
`app/web/viewmodels.py`, every setting out of `store.CONFIG_DEFAULTS`.

Gotcha: FastAPI 0.141 wraps included routers in `_IncludedRouter`, so naive
`app.routes` walking shows only 2 routes. Probe with TestClient.

## NEXT — the /design phase

The backend is feature-complete. Nothing in `app/config.py`, `app/db.py`,
`app/store.py`, `app/plex/`, `app/engine/` or `app/services/` should need to
change to build a real frontend.

- **Read `docs/API.md` first.** It is the whole contract: ~45 endpoints, every
  object shape, the auth/origin model, and ten notes on what a frontend must not
  get wrong (dry vs apply, `effective_mode`, the undo caveat, artwork proxying).
- **Replaceable surface:** `app/web/pages.py`, `app/web/templates/*.html`,
  `app/web/static/`. These exist only so the app is usable today.
- **Do not replace:** `app/web/api.py` (add to it if genuinely needed) or
  `app/web/viewmodels.py` — a new field goes in the viewmodel so the API and any
  UI stay in agreement, and `docs/API.md` is updated in the same commit.
- Poll `GET /api/status`; there is no websocket. `GET /api/schema` supplies every
  enumeration so no vocabulary is hardcoded in the frontend.

## Active decisions

- Rule = ONE `media_type` + 1..n libraries of that type.
- `tv_scope=series` collapses only when every episode of a show matched.
- Preview ephemeral; dry run recorded (candidates capped `DRY_RUN_ROW_LIMIT=1000`).
- Runs are background tasks; `run_and_wait()` for the scheduler and tests.
- Cooperative cancel via `manager.request_cancel()`.
- Safe mode rewrites the run row's `mode` via `store.set_run_mode()`, and has its
  own endpoint requiring `confirm` — never a checkbox in a long form.
- Only the scheduled tick prunes history.
- JSON API is the single contract; the UI is disposable and unstyled.
- Docker is single-stage; the session secret is never baked into the image.

## Known gaps / TODO

- `collapse_to_series` (tv_scope=series) is unit-tested but has no e2e path yet.
- Scheduler catch-up-on-boot is implemented but untested.
- Phase F was verified by a build only. **Not yet exercised at runtime:**
  `docker compose up -d`, /config ownership matching PUID/PGID on a real bind
  mount, config surviving down+up, and a run against a host-run mock at
  `http://host.docker.internal:32400`. Worth doing once before a release tag.
- No CI, no linter, no formatter configured.
