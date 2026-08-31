"""Rule validation and CRUD.

Everything that decides whether a rule is *coherent* lives here, so the API and
any future UI cannot construct a rule the engine would misread.

The trap this module exists to prevent (v1 hit it): saving a rule must never
treat an absent per-user override table as "the user unticked everyone". On a
single-user server the table is not rendered at all, and reading absence as
intent silently disabled the rule for everybody.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from .. import store
from ..engine.rules import AGE_UNITS, FILTER_FIELDS, MEDIA_TYPES, TV_SCOPES, Filter

log = logging.getLogger(__name__)

MAX_AGE_VALUE = 9999
MAX_MIN_VIEWS = 99


class RuleError(ValueError):
    """A rule the engine could not evaluate sensibly."""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: Any, *, default: int, low: int, high: int, label: str) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuleError(f"{label} must be a whole number.") from exc
    return max(low, min(high, number))


def parse_filters(entries: Any) -> list[Filter]:
    """Accept [{field, value}, …]. Unknown fields and blank values are dropped
    rather than rejected — a half-filled row in a form is not an error."""
    out: list[Filter] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        field = str(entry.get("field") or "").strip()
        value = str(entry.get("value") or "").strip()
        if field in FILTER_FIELDS and value:
            out.append(Filter(field, value))
    return out


def validate(payload: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn a submitted rule into storable columns, or raise RuleError."""
    current = existing or {}

    name = str(payload.get("name") or current.get("name") or "").strip()
    if not name:
        raise RuleError("Give the rule a name.")
    if len(name) > 120:
        name = name[:120]

    media_type = str(
        payload.get("media_type") or current.get("media_type") or "movie"
    ).strip()
    if media_type not in MEDIA_TYPES:
        raise RuleError(f"Media type must be one of {', '.join(MEDIA_TYPES)}.")

    age_unit = str(payload.get("age_unit") or current.get("age_unit") or "days").strip()
    if age_unit not in AGE_UNITS:
        raise RuleError(f"Timer unit must be one of {', '.join(AGE_UNITS)}.")

    age_value = _as_int(
        payload.get("age_value", current.get("age_value")),
        default=90, low=0, high=MAX_AGE_VALUE, label="Timer",
    )
    min_views = _as_int(
        payload.get("min_view_count", current.get("min_view_count")),
        default=1, low=1, high=MAX_MIN_VIEWS, label="Minimum view count",
    )

    tv_scope = str(payload.get("tv_scope") or current.get("tv_scope") or "episodes").strip()
    if tv_scope not in TV_SCOPES:
        raise RuleError(f"TV scope must be one of {', '.join(TV_SCOPES)}.")

    # --- library membership ------------------------------------------------
    raw_libraries = payload.get("library_ids")
    if raw_libraries is None:
        library_ids = [int(lib["id"]) for lib in current.get("libraries", [])]
    else:
        library_ids = _coerce_ids(raw_libraries)

    known = {int(lib["id"]): lib for lib in store.list_libraries()}
    unknown = [i for i in library_ids if i not in known]
    if unknown:
        raise RuleError("One of the selected libraries no longer exists.")
    if not library_ids:
        raise RuleError("Select at least one library for this rule to scan.")

    wrong = [
        known[i]["title"] for i in library_ids if str(known[i]["type"]) != media_type
    ]
    if wrong:
        raise RuleError(
            f"A {media_type} rule cannot scan {', '.join(wrong)}. Pick libraries "
            "of one media type, or create a second rule."
        )

    # --- gates that are meaningless off-type -------------------------------
    # Storing these as "on" for a movie rule would make the rule editor lie about
    # what the engine does, so they are normalised away at the boundary.
    if media_type == "show":
        require_series_complete = _as_bool(
            payload.get(
                "require_series_complete", current.get("require_series_complete", True)
            )
        )
    else:
        require_series_complete = False
        tv_scope = "episodes"

    return {
        "name": name,
        "enabled": _as_bool(payload.get("enabled", current.get("enabled", True))),
        "media_type": media_type,
        "age_value": age_value,
        "age_unit": age_unit,
        "min_view_count": min_views,
        "require_series_complete": int(require_series_complete),
        "skip_in_progress": int(
            _as_bool(payload.get("skip_in_progress", current.get("skip_in_progress", True)))
        ),
        "skip_now_playing": int(
            _as_bool(payload.get("skip_now_playing", current.get("skip_now_playing", True)))
        ),
        "clear_progress": int(
            _as_bool(payload.get("clear_progress", current.get("clear_progress", False)))
        ),
        "tv_scope": tv_scope,
        "include_filters": store.dump_filters(parse_filters(payload.get("include_filters", []))),
        "exclude_filters": store.dump_filters(parse_filters(payload.get("exclude_filters", []))),
        "library_ids": library_ids,
    }


def _coerce_ids(raw: Any) -> list[int]:
    if isinstance(raw, (str, int)):
        raw = [raw]
    out: list[int] = []
    for value in raw if isinstance(raw, Iterable) else []:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def create(payload: dict[str, Any]) -> dict[str, Any]:
    values = validate(payload)
    values["enabled"] = int(values["enabled"])
    values["sort_order"] = store.next_sort_order()
    rule_id = store.create_rule(**values)
    save_overrides(rule_id, payload)
    log.info("Created rule %s (%s)", values["name"], values["media_type"])
    return store.get_rule(rule_id) or {}


def update(rule_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    existing = store.get_rule(rule_id)
    if existing is None:
        raise RuleError("That rule no longer exists.")
    values = validate(payload, existing=existing)
    values["enabled"] = int(values["enabled"])
    store.update_rule(rule_id, **values)
    save_overrides(rule_id, payload)
    log.info("Saved rule %s", values["name"])
    return store.get_rule(rule_id) or {}


def toggle(rule_id: int) -> dict[str, Any]:
    rule = store.get_rule(rule_id)
    if rule is None:
        raise RuleError("That rule no longer exists.")
    store.update_rule(rule_id, enabled=0 if rule["enabled"] else 1)
    return store.get_rule(rule_id) or {}


def delete(rule_id: int) -> None:
    if store.get_rule(rule_id) is None:
        raise RuleError("That rule no longer exists.")
    store.delete_rule(rule_id)


def save_overrides(rule_id: int, payload: dict[str, Any]) -> None:
    """Persist per-user overrides — but ONLY when the caller actually sent them.

    `user_overrides` absent means "this form did not carry the table" (a
    single-user server never renders it). Treating that as "every user was
    unticked" is what silently disabled rules for everyone in v1, so absence is
    explicitly a no-op. An empty list is still a no-op for the same reason; to
    clear overrides, send them with `enabled: true` and no age.
    """
    overrides = payload.get("user_overrides")
    if not overrides:
        return
    if not isinstance(overrides, list):
        raise RuleError("user_overrides must be a list.")

    valid_users = {int(u["id"]) for u in store.list_users()}
    for entry in overrides:
        if not isinstance(entry, dict):
            continue
        try:
            user_id = int(entry.get("user_id"))
        except (TypeError, ValueError):
            continue
        if user_id not in valid_users:
            continue

        enabled = _as_bool(entry.get("enabled", True))
        raw_value = entry.get("age_value")
        value: int | None = None
        if raw_value not in (None, ""):
            try:
                value = max(0, min(MAX_AGE_VALUE, int(raw_value)))
            except (TypeError, ValueError):
                value = None
        unit = str(entry.get("age_unit") or "")
        store.set_rule_override(
            rule_id,
            user_id,
            enabled=enabled,
            age_value=value,
            # A blank number means "inherit the rule default" and is stored as
            # NULL rather than a copy of the default, so changing the default
            # later still moves everyone who never set their own.
            age_unit=unit if (value is not None and unit in AGE_UNITS) else None,
        )


def effective_thresholds(rule_id: int) -> list[dict[str, Any]]:
    """What each user's timer actually resolves to — the thing a UI should show
    rather than making someone reason about inheritance."""
    rule = store.get_rule(rule_id)
    if rule is None:
        raise RuleError("That rule no longer exists.")
    overrides = store.rule_overrides(rule_id)
    out = []
    for user in store.list_users():
        override = overrides.get(int(user["id"]))
        resolved = store.build_rule(rule, override)
        out.append(
            {
                "user_id": int(user["id"]),
                "user_title": user["title"],
                "included": not (override and not override["enabled"]),
                "age_value": resolved.age_value,
                "age_unit": resolved.age_unit,
                "inherited": not (override and override["age_value"] is not None),
            }
        )
    return out
