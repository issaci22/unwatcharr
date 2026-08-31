# Upgrading from Plex-Unwatcher v1

Unwatcharr 2.1 can import a Plex-Unwatcher v1 database. The import is **guided
from the setup wizard**, **read-only** on the source, and **one-way**.

---

## The guarantees

1. **The v1 database is opened read-only** (SQLite `mode=ro` URI) and is never
   written to. After an import it is byte-for-byte unchanged — this is asserted
   by a test.
2. **`session_secret` is never imported.** 2.1 generates its own on first boot.
   Carrying v1's over would mean every install that shared a v1 backup shared a
   session signing key.
3. **Plex tokens, password hashes and salts are not carried over either** —
   `SKIP_SETTINGS` drops them, and unknown v1 keys are ignored so a stale key
   cannot resurrect a setting 2.1 no longer has.
4. **Import is refused if this install already has data.** Rules or users
   present ⇒ refusal, unless you explicitly pass `force`.
5. **Re-importing is refused.** `migrated_from_v1` is recorded on success.

## How to do it

1. Make the v1 database reachable by the container: drop `plex-unwatcher.db`
   into your `/config` directory before first boot. `CONFIG_DIR` is the only
   place the wizard looks, at these three paths:
   `plex-unwatcher.db`, `plex-unwatcher/plex-unwatcher.db`, `v1/plex-unwatcher.db`.

2. Start the container and open the wizard. If a valid v1 database is found it
   shows what is inside — server name, and counts of rules, users, libraries,
   runs, actions and overrides — before you commit.

3. Accept the import. Then **check your rules**, **re-link your users** and
   **leave safe mode on** until a preview looks right.

Equivalent API calls: `GET /api/setup/v1-import` then `POST /api/setup/v1-import`.

## What gets mapped

| v1 | 2.1 |
|---|---|
| `settings` | copied key-by-key, skipping secrets and unknown keys |
| `libraries` | copied |
| `plex_users` | copied — **tokens are not**, so every user must be re-linked |
| `rules` | split into a `media_type` column + `rule_libraries` rows |
| `rule_users` | per-user overrides, preserved |
| `runs` sharing a batch id | collapsed into **one run with N passes** |
| `actions` | copied, re-pointed at the new run/pass ids |

The run collapse is the significant structural change. v1 wrote one `runs` row
per rule×user and tied them together with a batch id. 2.1 models that properly:
one run, many passes. A verified example — two v1 runs sharing batch
`f4912b4f2191` — becomes 1 run with 2 passes and a combined `scanned` of 234.

The rule reshape is the other one. In v1 a rule's media type was implicit in its
libraries. In 2.1 a rule is **one media type plus 1..n libraries of that type**,
which is what makes the TV-only gates (`require_series_complete`, `tv_scope`)
honest instead of silently ignored on movie rules.

## What you must redo by hand

- **Every Plex token.** Re-link users from the Users page.
- **The UI password.** Set a new one from Settings.
- **Your review of safe mode.** Import leaves safe mode at whatever the v1
  settings said; verify it before your first scheduled run.

## Behaviour differences worth knowing

- **`PLEX_TOKEN` is now actually consumed.** v1 advertised it in `.env.example`
  and `docker-compose.yml` and wired it to nothing. In 2.1 it is a genuine
  first-boot seed.
- **An absent or empty `user_overrides` never disables a rule.** This was a v1
  trap; 2.1 has a regression test pinning the correct behaviour.
- **Preview is ephemeral.** It records no run and cannot pollute your history.
- **Dry-run candidates are excluded from history** and pruned separately
  (`dry_run_keep_days`, default 14) from applied history (`history_keep_days`,
  default 365).
- **Only the scheduled tick prunes history.** A manual run never deletes rows.

## If the import fails

Nothing partial is left behind that you cannot clear by deleting
`/config/unwatcharr.db` and starting again — your v1 database is untouched, so
you can retry as many times as you like. Common refusals:

| Message | Meaning |
|---|---|
| "This does not look like a Plex-Unwatcher database (missing ...)" | Wrong file, or a v1 too old to have those tables. |
| "No file at ..." | The database is not at one of the searched paths inside `/config`. Check the bind mount and the filename. |
| Import refused, target not empty | You already have rules or users. Start from a clean `/config`. |
| Already imported | `migrated_from_v1` is set. Importing twice is not supported. |
