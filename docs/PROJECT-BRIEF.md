You are working on Version 2.1 of my Plex media utility application.

PROJECT NAME:
Unwatcharr

WORKSPACE:
D:\Documents\Claude Projects\Unwatcharr

PREVIOUS PROJECT / REFERENCE ONLY:
D:\Documents\Claude Projects\Plex-Unwatcher

IMPORTANT WORKSPACE RULE:
The Plex-Unwatcher workspace is READ-ONLY REFERENCE MATERIAL.

You may inspect, read, compare, and learn from files in:

D:\Documents\Claude Projects\Plex-Unwatcher

BUT YOU MUST NOT:
- modify anything in Plex-Unwatcher
- create files in Plex-Unwatcher
- delete anything in Plex-Unwatcher
- rename anything in Plex-Unwatcher
- run commands that modify its contents
- use it as the active development workspace

ALL NEW DEVELOPMENT MUST HAPPEN IN:

D:\Documents\Claude Projects\Unwatcharr


==================================================
PROJECT GOAL
==================================================

Build a substantially improved Version 2.1 of the original "Plex-Unwatcher" application.

The purpose of the application is:

Automatically mark watched Plex media as unwatched after a user-defined amount of time.

The application runs as a lightweight Docker container alongside Plex and communicates with Plex through its API.

It does NOT run inside Plex.

It should be designed specifically for self-hosted users and TrueNAS/homelab environments.

The new application should be called:

UNWATCHARR

Use the name consistently throughout the application, Docker configuration, documentation, UI branding, and project metadata.

The exact visual branding/logo can be handled separately by the UI/design work, but structure the application so branding can easily be changed later.


==================================================
FIRST TASK — STUDY THE PREVIOUS PROJECT
==================================================

Before writing code, inspect:

D:\Documents\Claude Projects\Plex-Unwatcher

Treat the existing project as a reference implementation.

Read its:

- CLAUDE.md
- application architecture
- database schema
- Plex integration
- authentication
- rules engine
- scheduler
- runner
- notification system
- templates
- CSS
- JavaScript
- Docker configuration
- tests
- migrations
- configuration handling

The existing CLAUDE.md contains important architectural decisions and known traps.

Pay particular attention to:

- Plex watch state being per-account
- per-user Plex tokens
- owner/home/managed users vs shared users
- the distinction between plex.tv account tokens and PMS server-scoped tokens
- safe mode
- dry runs
- rule evaluation
- scheduled runs
- history
- undo
- notification behavior
- SQLite migrations
- one-worker architecture
- mobile behavior
- security around Plex tokens
- Docker permissions
- timezone handling

Do not blindly copy the old implementation.

Determine which parts are:

1. worth directly reusing
2. worth adapting
3. technically obsolete
4. better redesigned
5. better rewritten completely

If the old implementation provides a strong foundation, reuse it.

If attempting to incrementally modify the old architecture would create unnecessary technical debt, recreate the application from scratch in the new workspace while preserving the proven behavior.

The goal is NOT "change as little code as possible."

The goal is:

BUILD THE BEST PRACTICAL VERSION 2.1 OF UNWATCHARR.


==================================================
IMPORTANT — PRESERVE CORRECT EXISTING BEHAVIOR
==================================================

The previous application already solved several difficult Plex-specific problems.

Do not regress them simply because the application is being redesigned.

Preserve the underlying correctness of:

- Plex per-account watch state
- per-user processing
- owner/home/managed user handling
- shared Plex user handling
- server-scoped tokens
- Plex account tokens
- scheduled processing
- manual processing
- dry-run behavior
- safe mode
- action history
- undo
- notifications
- rule evaluation
- show/episode handling
- lastViewedAt handling
- include/exclude filtering
- user-specific rule overrides
- timezone-aware scheduling
- SQLite persistence
- Docker operation
- PUID/PGID support

If you intentionally change any behavior, document why.


==================================================
VERSION 2.1 PRODUCT DIRECTION
==================================================

Unwatcharr should feel like a real self-hosted homelab application.

It should NOT feel like:

- a generic CRUD application
- an AI-generated dashboard
- a developer test interface
- a bare FastAPI admin panel
- a collection of forms with no visual hierarchy

It should feel like a polished application that belongs alongside:

- Plex
- Sonarr
- Radarr
- Bazarr
- Overseerr
- Tautulli
- Muxarr
- other mature self-hosted media applications

The application should be approachable for someone who installs a Docker container and opens the WebUI for the first time.


==================================================
FUNCTIONAL OBJECTIVES
==================================================

The new application should provide at minimum:

1. Plex connection/setup
2. Plex server discovery
3. Plex user discovery
4. Per-user watch-state management
5. Rule creation
6. Rule editing
7. Rule deletion
8. Rule enable/disable
9. Per-user rule overrides
10. Manual "Run Now"
11. Preview / dry-run
12. Scheduled execution
13. Safe mode
14. Run history
15. Action history
16. Undo
17. Notifications
18. Logs
19. Application settings
20. Authentication
21. Mobile-friendly UI
22. Docker deployment
23. Persistent configuration
24. Database migrations
25. Health/status information


==================================================
RULE SYSTEM
==================================================

The rule system should remain powerful.

Rules should be capable of defining things such as:

- media type
- libraries
- users
- age since last watched
- enabled/disabled
- include filters
- exclude filters
- genres
- collections
- labels
- show-level filtering
- episode-level filtering
- user-specific overrides

Preserve the existing behavior where appropriate.

An absent lastViewedAt must never be guessed.

If there is no trustworthy watched timestamp, skip the item.

Exclude filters must take precedence over include filters.

If show-level metadata is inherited by episodes, preserve that behavior.


==================================================
SAFE MODE
==================================================

Safe Mode is extremely important.

Safe Mode should be enabled by default on a fresh installation.

When Safe Mode is enabled:

- scheduled runs must not modify Plex
- manual runs must not modify Plex
- actions should still be calculated
- preview information should still be available
- the UI should make it extremely obvious that no changes will occur

The user should have to intentionally disable Safe Mode before actual modifications can occur.

Never hide this behavior behind a small settings checkbox.


==================================================
PREVIEW / DRY RUN
==================================================

The user should be able to preview what a rule would do before applying it.

A preview should clearly show:

- media title
- media type
- library
- Plex user
- last watched date
- calculated age
- rule that matched
- reason it matched
- reason an item was skipped when appropriate
- what action would happen

The UI should make the difference between:

"Would mark unwatched"

and

"Already skipped"

very obvious.

Previewing a rule should NOT send notifications.


==================================================
RUN HISTORY
==================================================

Maintain a useful run history.

Each run should record information such as:

- run ID
- start time
- end time
- trigger type
- dry-run/apply state
- rules processed
- users processed
- matched count
- changed count
- skipped count
- error count
- duration
- status

The user should be able to inspect historical runs.


==================================================
ACTION HISTORY / UNDO
==================================================

Every actual Plex change must have an audit trail.

Record:

- what media item changed
- which Plex user was affected
- when it changed
- which rule caused it
- previous known state
- resulting state

Undo must be supported.

IMPORTANT:

Plex's available API behavior may mean undo cannot perfectly restore the original watch timestamp or play count.

Do not pretend that it can.

The UI must clearly communicate what Undo actually restores.


==================================================
SCHEDULER
==================================================

Retain a reliable scheduler.

It must support:

- configurable interval
- timezone
- scheduled execution
- manual execution
- safe mode
- run locking
- no overlapping scans
- sensible handling when the container is offline

If settings affecting the schedule are changed, the scheduler must update without requiring a container restart whenever practical.

Do not create duplicate scheduled jobs.

Preserve coalescing behavior where appropriate.


==================================================
PLEX INTEGRATION
==================================================

Plex integration is one of the most important parts of this application.

Do not simplify it just to make the architecture easier.

Support the existing Plex user model correctly.

Remember:

Plex watch state is per-account.

A token only reads and writes the watch state of the account it belongs to.

There is no general impersonation parameter.

Therefore the application needs to process users using the appropriate tokens.

Preserve the existing handling for:

- Plex Home users
- managed users
- owner
- shared users
- server accounts
- server-scoped tokens
- Plex.tv account authentication

Do not expose Plex account tokens to the browser.

Do not create an endpoint that becomes an unrestricted proxy into arbitrary URLs.

Maintain host allowlisting for any server-side Plex.tv fetch functionality.


==================================================
DATABASE
==================================================

SQLite should remain the default database unless you discover a compelling reason not to use it.

This application is intended to be lightweight.

Do not introduce PostgreSQL or another external database merely for architectural fashion.

Database schema changes must use explicit migrations.

Never assume:

CREATE TABLE IF NOT EXISTS

will update an existing database.

Every schema change must:

- bump the schema version
- provide a forward migration
- work against an existing database
- work on a fresh install

Preserve existing data when upgrading from Version 1 where practical.


==================================================
UPGRADE / MIGRATION
==================================================

One of the major goals of Version 2.1 is to avoid making existing users start completely from zero.

Determine whether the existing Plex-Unwatcher database/configuration can be migrated.

If migration is practical:

- implement a migration path
- document it
- preserve rules
- preserve users
- preserve settings where compatible
- preserve history where practical

If migration is NOT practical:

- clearly document why
- provide a clean migration/import process where possible
- never silently destroy or overwrite existing data

Do not modify the old workspace to accomplish migration.


==================================================
DOCKER / TRUENAS
==================================================

This application is intended to run on a TrueNAS server.

Performance and image size matter.

Keep the application lightweight.

Avoid unnecessary dependencies.

The Docker image should:

- run as a non-root user where practical
- support PUID/PGID
- persist configuration under /config
- support TZ
- expose a configurable application port
- use one application worker unless architecture changes safely support multiple workers
- shut down cleanly
- recover cleanly after restart
- not require internet access at runtime for core functionality

Do not depend on CDNs for the core UI.

Vendor important frontend assets locally.


==================================================
DOCKER DESKTOP DEVELOPMENT
==================================================

My local machine has Docker Desktop.

Use Docker Desktop as an actual development tool.

Workspace:

D:\Documents\Claude Projects\Unwatcharr

Use Docker to:

- build the image
- start the application
- inspect logs
- test configuration
- reproduce container issues
- test persistent /config behavior
- test environment variables
- test permissions
- test restart behavior
- test networking to a mock Plex server

However:

BUILD FIRST.

Do not repeatedly rebuild/retest every tiny change.

Because Claude usage limits/credits matter, prioritize implementation over constant testing.

Recommended workflow:

1. Inspect
2. Plan
3. Implement a coherent feature group
4. Implement the related UI
5. Implement tests
6. Build
7. Run focused tests
8. Fix issues
9. Run a broader verification pass
10. Stop when the implementation is stable

Do not spend the majority of the task repeatedly running the same tests after insignificant changes.


==================================================
TESTING STRATEGY
==================================================

The previous project already had a strong testing approach.

Use that philosophy.

Prefer:

- pure logic tests
- recorded Plex fixtures
- mocked Plex server tests
- end-to-end tests against a mock Plex server
- focused regression tests for known bugs

Do not require a real Plex server for automated tests.

Create or preserve a mock Plex environment capable of verifying:

- Plex API calls
- users
- libraries
- watched state
- episode state
- unwatch operations
- per-user behavior

When testing a change, verify the actual request reaching the mock Plex server rather than trusting only the application's internal summary.


==================================================
KNOWN ARCHITECTURAL PRINCIPLES
==================================================

Maintain clean separation between:

WEB UI
↓
SERVICES / ORCHESTRATION
↓
RULE ENGINE / RUNNER
↓
PLEX CLIENT
↓
PLEX

The rule engine should remain as pure as reasonably possible.

It should not directly perform HTTP calls.

It should not directly own database access.

It should receive explicit inputs and produce deterministic decisions.

This makes Plex behavior testable and reduces regressions.


==================================================
LOGGING
==================================================

Logs should be useful to an actual user.

A run should communicate:

- what started
- whether it is DRY RUN or APPLY
- which rules are being processed
- which users are being processed
- how many items matched
- how many were skipped
- why items were skipped at an aggregate level
- how many changes occurred
- whether errors occurred
- how long it took

Do not flood the log with one entry for every skipped item.

Actual changes should remain individually auditable.


==================================================
API / WEB ARCHITECTURE
==================================================

You may modernize the web architecture if doing so produces a materially better application.

However, avoid unnecessary frontend complexity.

The application is intended for a TrueNAS server.

A lightweight architecture is preferred.

Do not introduce a massive JavaScript framework solely because it is fashionable.

Choose technology based on:

- maintainability
- performance
- Docker image size
- mobile support
- reliability
- ease of future development

If the existing FastAPI + Jinja/HTMX approach can support the new UI cleanly, it is perfectly acceptable to retain it.

If you determine that another architecture is substantially better, explain why before committing to a large rewrite.


==================================================
UI/UX REQUIREMENTS
==================================================

The UI itself will receive a dedicated design pass, but the backend must support a modern application.

Design the backend/API/templates so the UI can provide:

Dashboard:
- current status
- next scheduled run
- last run
- safe mode status
- connected Plex server
- affected users
- recent activity
- useful statistics

Rules:
- visual rule cards/table
- enabled/disabled state
- user assignment
- thresholds
- preview
- run now
- edit
- delete

Users:
- Plex avatar
- username
- user type
- connection/token state
- enabled state
- rule assignments

History:
- run history
- action history
- filters
- details
- undo

Settings:
- Plex
- scheduling
- notifications
- authentication
- system
- advanced settings

Logs:
- readable
- filterable
- recent activity


==================================================
AUTHENTICATION
==================================================

Retain application authentication.

Passwords should be securely hashed.

Session secrets must not be accidentally shared between installations.

Do not expose secrets to the frontend.

The Plex account token must never be sent to the browser.


==================================================
SECURITY
==================================================

Review the entire application for:

- secret exposure
- SSRF
- arbitrary URL fetching
- command injection
- unsafe template rendering
- CSRF concerns
- session security
- authentication bypass
- unsafe file handling
- Docker privilege issues
- token leakage
- log leakage

Especially review any endpoint that accepts URLs or remotely fetches Plex resources.


==================================================
DOCUMENTATION
==================================================

Create/update:

- README.md
- CLAUDE.md
- docker-compose.yml
- .env.example
- installation documentation
- upgrade/migration documentation
- configuration documentation

The new CLAUDE.md should explain the architecture and the important traps discovered during development.

Do not simply copy the old CLAUDE.md.

Create a Version 2.1-specific engineering guide.


==================================================
DEVELOPMENT PHILOSOPHY
==================================================

Do not ask me unnecessary questions.

Make reasonable engineering decisions yourself.

When there are multiple viable approaches:

- prefer the simplest robust solution
- favor low resource usage
- favor maintainability
- favor backwards compatibility
- favor self-hosted reliability

Do not prematurely optimize.

Do not add features simply because another application has them.

The purpose of Version 2.1 is to make Unwatcharr:

- easier to understand
- safer
- more polished
- more reliable
- easier to configure
- easier to maintain
- better suited to long-term use


==================================================
IMPORTANT: BUILD FIRST, TEST SECOND
==================================================

I specifically want development to prioritize BUILDING.

Do not spend excessive Claude usage repeatedly testing incomplete intermediate states.

Instead:

- inspect the old project thoroughly
- formulate a plan
- implement the major architecture
- implement the major functionality
- implement the UI-supporting backend
- implement migrations
- implement tests
- then perform meaningful verification

Use Docker Desktop throughout the development process, but don't turn every small edit into a full test cycle.


==================================================
START HERE
==================================================

Before modifying anything:

1. Confirm the active workspace is:

   D:\Documents\Claude Projects\Unwatcharr

2. Inspect the current contents of that workspace.

3. Inspect the previous project:

   D:\Documents\Claude Projects\Plex-Unwatcher

4. Read the old CLAUDE.md completely.

5. Map the old architecture.

6. Determine what can be reused.

7. Determine what should be rewritten.

8. Create an implementation plan.

9. Begin building Version 2.1.

Do NOT modify the Plex-Unwatcher workspace under any circumstances.

The final result should be a complete, runnable Unwatcharr application rather than merely a prototype or UI mockup.