"""Executes rules against Plex.

A "run" is one press of Run Now, or one scheduler tick. It fans out into a
`run_passes` row per rule per user, because watch state is per-token and each
user must be scanned with their own.

Two modes:
  dry    -- record what would change and touch nothing
  apply  -- unscrobble each match, writing a durable action row per item FIRST,
            so an interrupted run still leaves an undo trail

Runs execute as a background task. The HTTP layer starts one and returns a run
id immediately; a sweep over a large library takes minutes and must not be tied
to a request that a reverse proxy will time out.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from .. import store
from ..plex.client import PlexAuthError, PlexError, PlexServer
from ..timeutil import now as _now
from .collect import collect
from .rules import (
    Decision,
    EvalContext,
    Rule,
    collapse_to_series,
    describe_threshold,
    evaluate,
    summarise_skips,
)

log = logging.getLogger(__name__)

# Applied changes are always logged in full -- that is the audit trail. Dry-run
# matches are sampled, since a first pass over a large library can match
# thousands and the preview already lists every one.
DRY_RUN_LOG_LIMIT = 25

# Cap on dry-run candidate rows written per pass. They exist so a scheduled dry
# run can be inspected afterwards; without a cap a nightly safe-mode sweep over
# a 20k library would grow the database without bound.
DRY_RUN_ROW_LIMIT = 1000


class RunCancelled(Exception):
    """Raised inside a run when the user asks it to stop."""


@dataclass
class PassResult:
    pass_id: int
    rule_id: int
    rule_name: str
    user_id: int
    user_title: str
    mode: str
    status: str = "ok"
    scanned: int = 0
    matched: int = 0
    applied: int = 0
    failed: int = 0
    skipped: int = 0
    error: str | None = None
    skip_summary: list[tuple[str, int]] = field(default_factory=list)
    sample: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    run_id: int
    uid: str
    mode: str
    trigger: str
    status: str = "ok"
    error: str | None = None
    passes: list[PassResult] = field(default_factory=list)

    @property
    def total_scanned(self) -> int:
        return sum(p.scanned for p in self.passes)

    @property
    def total_matched(self) -> int:
        return sum(p.matched for p in self.passes)

    @property
    def total_applied(self) -> int:
        return sum(p.applied for p in self.passes)

    @property
    def total_failed(self) -> int:
        return sum(p.failed for p in self.passes)

    @property
    def total_skipped(self) -> int:
        return sum(p.skipped for p in self.passes)

    @property
    def errors(self) -> list[PassResult]:
        return [p for p in self.passes if p.status == "error"]


class RunManager:
    """Serialises runs and exposes progress.

    Module-level singleton. The lock, the progress dict and the cancel flag are
    all process-local -- which is the other half of the one-worker rule.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._cancel = False
        self.progress: dict[str, Any] | None = None
        self.last_result: RunResult | None = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def request_cancel(self) -> bool:
        """Cooperative stop. Returns False when nothing is running."""
        if not self.busy:
            return False
        self._cancel = True
        log.warning("Stop requested -- the run will finish its current item and halt.")
        return True

    def _check_cancel(self) -> None:
        if self._cancel:
            raise RunCancelled()

    def _set_progress(self, **fields: Any) -> None:
        if self.progress is None:
            self.progress = {}
        self.progress.update(fields)

    async def start(
        self,
        *,
        rule_ids: Sequence[int] | None = None,
        user_ids: Sequence[int] | None = None,
        mode: str = "dry",
        trigger: str = "manual",
    ) -> dict[str, Any]:
        """Kick a run off in the background and return its identity at once."""
        if self.busy:
            raise RuntimeError("A run is already in progress.")

        run_id, uid = store.create_run(mode=mode, trigger=trigger)
        self.progress = {
            "run_id": run_id,
            "uid": uid,
            "mode": mode,
            "trigger": trigger,
            "phase": "starting",
            "done": 0,
            "total": 0,
            "current": None,
        }
        self._task = asyncio.create_task(
            self._guarded(
                run_id=run_id,
                uid=uid,
                rule_ids=rule_ids,
                user_ids=user_ids,
                mode=mode,
                trigger=trigger,
            )
        )
        return {"run_id": run_id, "uid": uid, "mode": mode, "trigger": trigger}

    async def run_and_wait(
        self,
        *,
        rule_ids: Sequence[int] | None = None,
        user_ids: Sequence[int] | None = None,
        mode: str = "dry",
        trigger: str = "manual",
    ) -> RunResult:
        """Start a run and wait for it. Used by the scheduler and by tests."""
        await self.start(
            rule_ids=rule_ids, user_ids=user_ids, mode=mode, trigger=trigger
        )
        assert self._task is not None
        await self._task
        if self.last_result is None:
            raise RuntimeError("The run produced no result.")
        return self.last_result

    async def _guarded(self, **kwargs: Any) -> None:
        run_id = int(kwargs["run_id"])
        async with self._lock:
            self._cancel = False
            try:
                result = await self._run(**kwargs)
                self.last_result = result
            except RunCancelled:
                store.finish_run(run_id, status="cancelled", error="Stopped by request")
                log.warning("Run stopped by request.")
            except Exception as exc:  # noqa: BLE001 - a run must never kill the app
                store.finish_run(run_id, status="error", error=str(exc)[:500])
                log.exception("Run failed: %s", exc)
            finally:
                self._cancel = False
                self.progress = None

    # ------------------------------------------------------------------

    async def _run(
        self,
        *,
        run_id: int,
        uid: str,
        rule_ids: Sequence[int] | None,
        user_ids: Sequence[int] | None,
        mode: str,
        trigger: str,
    ) -> RunResult:
        config = store.all_config()

        # Safe mode is the master switch: it downgrades every run to a dry run,
        # so a mis-tuned rule cannot do damage while you are still setting up.
        if config.get("safe_mode") and mode == "apply":
            log.warning(
                "Safe mode is ON, so this run is forced to a dry run. Nothing in "
                "Plex will be changed. Turn it off in Settings when the previews "
                "look right."
            )
            mode = "dry"
            store.set_run_mode(run_id, mode)

        result = RunResult(run_id=run_id, uid=uid, mode=mode, trigger=trigger)
        self._set_progress(mode=mode, phase="preparing")

        plex_url = config.get("plex_url")
        if not plex_url:
            raise RuntimeError("Plex is not configured yet. Finish setup first.")

        rules = store.list_rules(enabled_only=True)
        if rule_ids is not None:
            wanted = {int(r) for r in rule_ids}
            rules = [r for r in rules if int(r["id"]) in wanted]
        rules = [r for r in rules if r.get("libraries")]
        if not rules:
            log.info("No enabled rules with libraries to run.")
            store.finish_run(run_id, status="ok", rules_processed=0, users_processed=0)
            return result

        users = store.runnable_users()
        if user_ids is not None:
            wanted_users = {int(u) for u in user_ids}
            users = [u for u in users if int(u["id"]) in wanted_users]
        if not users:
            log.warning(
                "No users have a usable token, so there is nothing to scan. Link "
                "at least one account on the Users page."
            )
            store.finish_run(
                run_id,
                status="ok",
                rules_processed=len(rules),
                users_processed=0,
                error="No users with a usable token.",
            )
            return result

        server = PlexServer(
            str(plex_url),
            str(config.get("client_identifier") or "unwatcharr"),
            verify_ssl=bool(config.get("plex_verify_ssl")),
        )
        try:
            owner = store.owner_user() or users[0]
            now_playing = await server.now_playing_keys(str(owner.get("token") or ""))
            if now_playing:
                log.info(
                    "%d item(s) currently streaming will be protected.", len(now_playing)
                )

            total = len(rules) * len(users)
            self._set_progress(phase="scanning", done=0, total=total)

            if mode == "dry":
                log.info(
                    "===== DRY RUN starting (%s) - %d rule(s) x %d user(s). "
                    "Nothing will be changed in Plex. =====",
                    trigger,
                    len(rules),
                    len(users),
                )
            else:
                log.info(
                    "===== RUN starting (%s) - %d rule(s) x %d user(s). Matching "
                    "items WILL be marked unwatched. =====",
                    trigger,
                    len(rules),
                    len(users),
                )

            for rule_row in rules:
                overrides = store.rule_overrides(int(rule_row["id"]))
                for user in users:
                    self._check_cancel()
                    override = overrides.get(int(user["id"]))
                    # An explicit per-user opt-out on this rule.
                    if override and not override.get("enabled", 1):
                        self._advance()
                        continue

                    outcome = await self._run_pass(
                        server=server,
                        run_id=run_id,
                        rule_row=rule_row,
                        user=user,
                        override=override,
                        mode=mode,
                        now_playing=now_playing,
                        delay_ms=int(config.get("request_delay_ms") or 0),
                        server_side_filters=bool(config.get("server_side_filters")),
                    )
                    result.passes.append(outcome)
                    self._advance(current=f"{rule_row['name']} / {user['title']}")
        finally:
            await server.aclose()

        store.finish_run(
            run_id,
            status="error" if result.errors else "ok",
            rules_processed=len(rules),
            users_processed=len(users),
            scanned=result.total_scanned,
            matched=result.total_matched,
            applied=result.total_applied,
            failed=result.total_failed,
            skipped=result.total_skipped,
            error=result.errors[0].error if result.errors else None,
        )

        if mode == "dry":
            log.info(
                "===== DRY RUN finished: checked %d item(s), %d would be marked "
                "unwatched. Nothing was changed. =====",
                result.total_scanned,
                result.total_matched,
            )
        else:
            log.info(
                "===== RUN finished: checked %d item(s), marked %d unwatched%s. =====",
                result.total_scanned,
                result.total_applied,
                f", {result.total_failed} failed" if result.total_failed else "",
            )
        return result

    def _advance(self, current: str | None = None) -> None:
        done = (self.progress or {}).get("done", 0) + 1
        self._set_progress(done=done)
        if current:
            self._set_progress(current=current)

    # ------------------------------------------------------------------

    async def _run_pass(
        self,
        *,
        server: PlexServer,
        run_id: int,
        rule_row: dict[str, Any],
        user: dict[str, Any],
        override: dict[str, Any] | None,
        mode: str,
        now_playing: set[str],
        delay_ms: int,
        server_side_filters: bool,
    ) -> PassResult:
        rule = store.build_rule(rule_row, override)
        pass_id = store.create_pass(
            run_id=run_id,
            rule_id=int(rule_row["id"]),
            rule_name=str(rule_row["name"]),
            user_id=int(user["id"]),
            user_title=str(user["title"]),
        )
        outcome = PassResult(
            pass_id=pass_id,
            rule_id=int(rule_row["id"]),
            rule_name=str(rule_row["name"]),
            user_id=int(user["id"]),
            user_title=str(user["title"]),
            mode=mode,
        )
        token = str(user.get("token") or "")
        libraries = rule_row.get("libraries") or []

        log.info(
            "[%s] Scanning %s for %s -- %s anything watched more than %s ago",
            rule.name,
            ", ".join(str(l["title"]) for l in libraries) or "nothing",
            user["title"],
            "would unwatch" if mode == "dry" else "unwatching",
            describe_threshold(rule.age_value, rule.age_unit),
        )

        try:
            ctx = EvalContext(now=_now(), now_playing=now_playing)
            collected = await collect(
                server=server,
                token=token,
                rule=rule,
                ctx=ctx,
                libraries=libraries,
                server_side_filters=server_side_filters,
            )
            matched, skipped = evaluate(collected.items, rule, ctx)
            if rule.media_type == "show" and rule.tv_scope == "series":
                matched = collapse_to_series(matched, collected.shows)

            outcome.scanned = len(collected.items)
            outcome.matched = len(matched)
            outcome.skipped = len(skipped)
            outcome.skip_summary = summarise_skips(skipped)
            outcome.sample = [d.item.display_title for d in matched[:5]]

            log.info(
                "[%s] Checked %d item(s) for %s: %d match, %d left alone",
                rule.name,
                outcome.scanned,
                user["title"],
                outcome.matched,
                outcome.skipped,
            )
            # Aggregated reasons only. One line per skipped item would drown the
            # log on every scheduled run over a library where nothing matches.
            for reason, count in outcome.skip_summary[:4]:
                log.info("[%s]   left alone - %s: %d", rule.name, reason, count)

            if mode == "apply":
                await self._apply(
                    server=server,
                    run_id=run_id,
                    pass_id=pass_id,
                    rule_row=rule_row,
                    rule=rule,
                    user=user,
                    matched=matched,
                    token=token,
                    delay_ms=delay_ms,
                    outcome=outcome,
                )
            else:
                self._record_candidates(
                    run_id=run_id,
                    pass_id=pass_id,
                    rule_row=rule_row,
                    rule=rule,
                    user=user,
                    matched=matched,
                )

            store.finish_pass(
                pass_id,
                status="ok",
                scanned=outcome.scanned,
                matched=outcome.matched,
                applied=outcome.applied,
                failed=outcome.failed,
                skipped=outcome.skipped,
                skip_summary=json.dumps(outcome.skip_summary),
            )

        except RunCancelled:
            store.finish_pass(pass_id, status="cancelled")
            raise

        except PlexAuthError as exc:
            # The token died. Flag it so the Users page shows why, rather than
            # failing silently every six hours forever.
            store.set_user_token_status(int(user["id"]), "invalid")
            outcome.status, outcome.error = "error", str(exc)
            store.finish_pass(pass_id, status="error", error=str(exc)[:500])
            log.error("Pass %s failed: %s", pass_id, exc)

        except Exception as exc:  # noqa: BLE001 - one bad rule must not kill the run
            outcome.status, outcome.error = "error", str(exc)
            store.finish_pass(pass_id, status="error", error=str(exc)[:500])
            log.exception("Pass %s failed: %s", pass_id, exc)

        return outcome

    def _record_candidates(
        self,
        *,
        run_id: int,
        pass_id: int,
        rule_row: dict[str, Any],
        rule: Rule,
        user: dict[str, Any],
        matched: Sequence[Decision],
    ) -> None:
        for index, decision in enumerate(matched):
            if index < DRY_RUN_ROW_LIMIT:
                store.add_action(
                    run_id=run_id,
                    pass_id=pass_id,
                    rule_id=int(rule_row["id"]),
                    rule_name=str(rule_row["name"]),
                    user=user,
                    item=decision.item,
                    status="candidate",
                )
            if index < DRY_RUN_LOG_LIMIT:
                log.info(
                    "[%s]   would unwatch: %s", rule.name, decision.item.display_title
                )
        if len(matched) > DRY_RUN_LOG_LIMIT:
            log.info(
                "[%s]   ... and %d more (open Preview for the full list)",
                rule.name,
                len(matched) - DRY_RUN_LOG_LIMIT,
            )

    async def _apply(
        self,
        *,
        server: PlexServer,
        run_id: int,
        pass_id: int,
        rule_row: dict[str, Any],
        rule: Rule,
        user: dict[str, Any],
        matched: Sequence[Decision],
        token: str,
        delay_ms: int,
        outcome: PassResult,
    ) -> None:
        delay = max(0.0, delay_ms / 1000.0)
        for index, decision in enumerate(matched):
            self._check_cancel()
            item = decision.item
            # Written BEFORE the call so an interrupted run still leaves a trail.
            action_id = store.add_action(
                run_id=run_id,
                pass_id=pass_id,
                rule_id=int(rule_row["id"]),
                rule_name=str(rule_row["name"]),
                user=user,
                item=item,
                status="candidate",
            )
            try:
                await server.unscrobble(item.rating_key, token)
                if rule.clear_progress:
                    await server.clear_progress(item.rating_key, token)
                store.mark_action(action_id, "applied")
                outcome.applied += 1
                # Logged in full: this is the audit trail of what actually
                # changed, and the request pacing keeps it from being a flood.
                log.info(
                    "[%s]   UNWATCHED for %s: %s",
                    rule.name,
                    user.get("title"),
                    item.display_title,
                )
            except PlexAuthError:
                store.mark_action(action_id, "failed", "Token rejected")
                outcome.failed += 1
                raise
            except PlexError as exc:
                store.mark_action(action_id, "failed", str(exc)[:300])
                outcome.failed += 1
                log.warning("Could not unwatch %s: %s", item.display_title, exc)

            if delay and index < len(matched) - 1:
                await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

async def undo_action(action_id: int) -> None:
    """Re-mark a single item watched.

    Plex records a FRESH play; it will not restore the original lastViewedAt or
    viewCount. This recovers watched status, not history. Say so in the UI
    rather than implying otherwise.
    """
    action = store.get_action(action_id)
    if action is None:
        raise RuntimeError("That history entry no longer exists.")
    if action["status"] != "applied":
        raise RuntimeError("Only applied changes can be undone.")

    user = store.get_user(int(action["user_id"])) if action["user_id"] else None
    if not user or not user.get("token"):
        raise RuntimeError(
            f"No usable token for {action['user_title'] or 'that user'}, so this "
            "cannot be undone. Re-link them on the Users page."
        )

    config = store.all_config()
    server = PlexServer(
        str(config["plex_url"]),
        str(config.get("client_identifier") or "unwatcharr"),
        verify_ssl=bool(config.get("plex_verify_ssl")),
    )
    try:
        await server.scrobble(str(action["rating_key"]), str(user["token"]))
        store.mark_action_undone(action_id)
        log.info(
            "UNDONE for %s: %s (watched again; the original date and play count "
            "cannot be restored)",
            action["user_title"],
            action["title"],
        )
    finally:
        await server.aclose()


async def undo_run(run_id: int) -> tuple[int, int]:
    """Undo every applied change in a run. Returns (undone, failed)."""
    actions = store.undoable_actions(run_id)
    if not actions:
        return 0, 0

    config = store.all_config()
    server = PlexServer(
        str(config["plex_url"]),
        str(config.get("client_identifier") or "unwatcharr"),
        verify_ssl=bool(config.get("plex_verify_ssl")),
    )
    delay = max(0.0, int(config.get("request_delay_ms") or 0) / 1000.0)
    undone = failed = 0
    tokens: dict[int, str | None] = {}
    try:
        for action in actions:
            user_id = int(action["user_id"] or 0)
            if user_id not in tokens:
                user = store.get_user(user_id)
                tokens[user_id] = (user or {}).get("token")
            token = tokens[user_id]
            if not token:
                failed += 1
                continue
            try:
                await server.scrobble(str(action["rating_key"]), str(token))
                store.mark_action_undone(int(action["id"]))
                undone += 1
            except PlexError as exc:
                log.warning("Undo failed for %s: %s", action["title"], exc)
                failed += 1
            if delay:
                await asyncio.sleep(delay)
    finally:
        await server.aclose()
    log.info("Undo finished: %d marked watched again, %d failed.", undone, failed)
    return undone, failed


manager = RunManager()
