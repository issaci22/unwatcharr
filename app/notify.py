"""Run summaries pushed to a webhook.

Three shapes, because the three things people actually point this at want
different payloads: a Discord webhook wants {"content": …}, ntfy wants a plain
text body, and anything else gets structured JSON it can do what it likes with.

Two behaviours to keep:
  - a dead webhook logs a warning and NEVER fails the run that triggered it
  - a batch that matched nothing and broke nothing sends nothing at all

Previewing a rule deliberately does not notify — preview never creates a run,
so it cannot reach this module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from . import store
from .config import APP_NAME

if TYPE_CHECKING:
    from .engine.runner import RunResult

log = logging.getLogger(__name__)

NOTIFY_KINDS = ("webhook", "discord", "ntfy")


def summarise(result: "RunResult") -> str:
    verb = "Would unwatch" if result.mode == "dry" else "Unwatched"
    lines = [
        f"**{APP_NAME}** — {result.trigger} run ({result.mode})",
        f"{verb} {result.total_matched} item(s) across {len(result.passes)} "
        f"rule/user pass(es); scanned {result.total_scanned}.",
    ]
    if result.mode == "apply" and result.total_applied != result.total_matched:
        lines.append(
            f"Applied {result.total_applied}, failed {result.total_failed}."
        )

    for outcome in result.passes:
        if outcome.status == "error":
            lines.append(
                f"- ERROR {outcome.rule_name} / {outcome.user_title}: {outcome.error}"
            )
        elif outcome.matched:
            count = outcome.applied if result.mode == "apply" else outcome.matched
            lines.append(f"- {outcome.rule_name} / {outcome.user_title}: {count}")

    return "\n".join(lines)


def _payload(kind: str, result: "RunResult", text: str) -> tuple[Any, dict[str, str]]:
    if kind == "discord":
        # Discord hard-caps message content at 2000 characters.
        return {"content": text[:1900]}, {"Content-Type": "application/json"}
    if kind == "ntfy":
        return text.encode("utf-8"), {
            "Title": APP_NAME,
            "Tags": "arrows_counterclockwise",
        }
    return (
        {
            "source": "unwatcharr",
            "run_uid": result.uid,
            "mode": result.mode,
            "trigger": result.trigger,
            "scanned": result.total_scanned,
            "matched": result.total_matched,
            "applied": result.total_applied,
            "failed": result.total_failed,
            "skipped": result.total_skipped,
            "text": text,
            "passes": [
                {
                    "rule": p.rule_name,
                    "user": p.user_title,
                    "status": p.status,
                    "scanned": p.scanned,
                    "matched": p.matched,
                    "applied": p.applied,
                    "failed": p.failed,
                    "error": p.error,
                }
                for p in result.passes
            ],
        },
        {"Content-Type": "application/json"},
    )


async def send(result: "RunResult") -> None:
    config = store.all_config()
    if not config.get("notify_enabled") or not config.get("notify_url"):
        return
    if result.mode == "dry" and not config.get("notify_on_dry_run"):
        return
    if config.get("notify_on_error_only") and not result.errors:
        return
    # Nothing happened and nothing broke -- no need to ping anyone.
    if not result.total_matched and not result.errors:
        return

    await _post(
        str(config.get("notify_kind") or "webhook"),
        str(config["notify_url"]),
        result,
        summarise(result),
    )


async def _post(kind: str, url: str, result: "RunResult", text: str) -> None:
    body, headers = _payload(kind, result, text)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if isinstance(body, bytes):
                response = await client.post(url, content=body, headers=headers)
            else:
                response = await client.post(url, json=body, headers=headers)
        if response.status_code >= 400:
            log.warning(
                "Notification rejected with HTTP %s: %s",
                response.status_code,
                response.text[:200],
            )
        else:
            log.info("Sent the run summary to the configured %s endpoint.", kind)
    except httpx.HTTPError as exc:
        # A dead webhook must never fail the run that triggered it.
        log.warning("Could not send the notification: %s", exc)


async def send_test(kind: str, url: str) -> str:
    """Fire a sample notification from the settings page."""
    from .engine.runner import PassResult, RunResult

    result = RunResult(run_id=0, uid="test", mode="dry", trigger="test")
    result.passes.append(
        PassResult(
            pass_id=0,
            rule_id=0,
            rule_name="Test rule",
            user_id=0,
            user_title="Test user",
            mode="dry",
            scanned=42,
            matched=3,
        )
    )
    text = f"**{APP_NAME}** — test notification. If you can read this, it works."
    body, headers = _payload(kind, result, text)
    async with httpx.AsyncClient(timeout=15.0) as client:
        if isinstance(body, bytes):
            response = await client.post(url, content=body, headers=headers)
        else:
            response = await client.post(url, json=body, headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
    return f"Sent (HTTP {response.status_code})."
