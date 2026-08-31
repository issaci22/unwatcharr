"""The scheduled sweep.

APScheduler on the app's own event loop, one job. The job is rescheduled
whenever settings are saved, so changing the interval takes effect immediately
rather than after a container restart.

Timezone comes from `timeutil.local_tz()`, which returns None on an unresolvable
TZ so APScheduler picks a default. Passing a bad value through raises at startup
and takes the whole app down over a typo'd zone name.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .. import store
from ..timeutil import local_tz, now as _now
from .runner import manager

log = logging.getLogger(__name__)

JOB_ID = "unwatch-sweep"
CATCH_UP_JOB_ID = "unwatch-catch-up"
_scheduler: AsyncIOScheduler | None = None


async def _tick(trigger: str = "schedule") -> None:
    if manager.busy:
        # A manual run is in flight. Skipping is right: the next tick is minutes
        # away, and queueing would just double-scan the same library.
        log.info("Scheduled run skipped -- a run is already in progress.")
        return

    if not store.get_config("setup_complete"):
        log.debug("Scheduled run skipped -- setup is not finished.")
        return

    log.info("Scheduled run starting.")
    store.set_config("last_scheduled_run_at", _now())
    try:
        result = await manager.run_and_wait(mode="apply", trigger=trigger)
    except Exception as exc:  # noqa: BLE001 - the scheduler must survive anything
        log.exception("Scheduled run failed: %s", exc)
        return

    from .. import notify

    await notify.send(result)

    # Only the scheduled tick prunes, so a burst of manual runs never trims
    # history out from under someone reading the history page.
    config = store.all_config()
    removed = store.prune_history(
        keep_days=int(config.get("history_keep_days") or 365),
        dry_keep_days=int(config.get("dry_run_keep_days") or 14),
    )
    if any(removed.values()):
        log.info("Pruned old history: %s", removed)


def _build_trigger() -> IntervalTrigger | CronTrigger | None:
    config = store.all_config()
    if not config.get("schedule_enabled"):
        return None

    if config.get("schedule_kind") == "cron":
        expression = str(config.get("schedule_cron") or "0 4 * * *").strip()
        try:
            return CronTrigger.from_crontab(expression, timezone=local_tz())
        except ValueError as exc:
            log.error(
                "Cron expression %r is not valid (%s); falling back to every 6 hours.",
                expression,
                exc,
            )
            return IntervalTrigger(hours=6)

    hours = int(config.get("schedule_hours") or 6)
    return IntervalTrigger(hours=max(1, hours))


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone=local_tz())
    _scheduler.start()
    reschedule()
    _schedule_catch_up()


def reschedule() -> None:
    """Apply the current schedule settings. Safe to call repeatedly.

    Any settings save MUST call this, or a new interval only takes effect after
    a restart.
    """
    if _scheduler is None:
        return
    if _scheduler.get_job(JOB_ID):
        _scheduler.remove_job(JOB_ID)

    trigger = _build_trigger()
    if trigger is None:
        log.info("Scheduled runs are turned off.")
        return

    _scheduler.add_job(
        _tick,
        trigger=trigger,
        id=JOB_ID,
        max_instances=1,
        # A backlog built up while the container was down collapses into a
        # single run rather than firing once per missed interval.
        coalesce=True,
        misfire_grace_time=3600,
    )
    log.info("Next scheduled run: %s", next_run_time() or "unknown")


def _schedule_catch_up() -> None:
    """Run once shortly after boot if the container missed a whole interval.

    APScheduler's coalescing only helps for jobs it knew about while the process
    was alive; a container that was off for two days simply starts counting
    again from now. This closes that gap without letting a restart loop hammer
    Plex -- the run is one-shot and only fires when genuinely overdue.
    """
    if _scheduler is None:
        return
    config = store.all_config()
    if not config.get("schedule_enabled") or not config.get("catch_up_missed_runs"):
        return
    if not config.get("setup_complete"):
        return

    last = int(config.get("last_scheduled_run_at") or 0)
    if not last:
        return  # never run before; the normal schedule is soon enough

    interval = _expected_interval_seconds(config)
    overdue_by = _now() - last - interval
    if overdue_by <= 0:
        return

    log.info(
        "The last scheduled run was %s ago, which is overdue. Catching up "
        "shortly after startup.",
        _pretty_seconds(_now() - last),
    )
    _scheduler.add_job(
        _tick,
        trigger=IntervalTrigger(seconds=60),
        id=CATCH_UP_JOB_ID,
        max_instances=1,
        coalesce=True,
        # One-shot: remove itself after the first fire.
        next_run_time=datetime.now(tz=local_tz()),
        kwargs={"trigger": "catch-up"},
        misfire_grace_time=300,
    )
    asyncio.get_event_loop().call_later(120, _drop_catch_up)


def _drop_catch_up() -> None:
    if _scheduler is not None and _scheduler.get_job(CATCH_UP_JOB_ID):
        _scheduler.remove_job(CATCH_UP_JOB_ID)


def _expected_interval_seconds(config: dict) -> int:
    if config.get("schedule_kind") == "cron":
        # Cron windows vary; a day is a reasonable "clearly overdue" threshold.
        return 86400
    return max(1, int(config.get("schedule_hours") or 6)) * 3600


def _pretty_seconds(seconds: int) -> str:
    hours = seconds // 3600
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def next_run_time() -> datetime | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(JOB_ID)
    return getattr(job, "next_run_time", None) if job else None


def is_running() -> bool:
    return _scheduler is not None and _scheduler.running


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
