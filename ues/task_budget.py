from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

UNKNOWN_QUOTA_WINDOW_POLICIES = {
    "DENY",
    "ALLOW_UNLESS_DIRECT_CEILING_REACHED",
}
# Backward-compatible alias for callers/config that have not yet renamed the
# policy field. The semantics are current-quota-window semantics, not lifetime.
UNKNOWN_LIFETIME_POLICIES = UNKNOWN_QUOTA_WINDOW_POLICIES


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_provider_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def observe_rolling_quota_window(
    tasks: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    window_seconds: int = 24 * 60 * 60,
    timestamp_fields: tuple[str, ...] = ("createTime", "createdAt", "created_at", "create_time"),
) -> dict[str, Any]:
    """Observe only the provider tasks inside the current rolling quota window.

    Historical tasks remain visible to callers for audit/reconciliation, but
    they never consume current quota-window capacity. Missing/unparseable
    timestamps make complete window consumption unknown instead of silently
    treating historical inventory as current consumption.
    """

    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    observed_now = _utc(now or datetime.now(timezone.utc))
    cutoff = observed_now - timedelta(seconds=window_seconds)
    current = 0
    historical = 0
    unknown = 0
    total = 0
    for task in tasks:
        total += 1
        timestamp = None
        for field in timestamp_fields:
            timestamp = _parse_provider_timestamp(task.get(field))
            if timestamp is not None:
                break
        if timestamp is None:
            unknown += 1
            continue
        if cutoff < timestamp <= observed_now:
            current += 1
        else:
            historical += 1

    complete = unknown == 0
    return {
        "quota_window_kind": "ROLLING",
        "quota_window_seconds": window_seconds,
        "quota_window_started_at": cutoff.isoformat().replace("+00:00", "Z"),
        "quota_window_observed_at": observed_now.isoformat().replace("+00:00", "Z"),
        "quota_window_consumption_known": complete,
        "proven_quota_window_used": current if complete else None,
        "current_window_enumerated_tasks": current,
        "historical_outside_window_tasks": historical,
        "unknown_timestamp_tasks": unknown,
        "provider_inventory_total": total,
        "historical_usage_affects_capacity": False,
    }


def evaluate_task_budget(
    *,
    project: str,
    ceiling: int,
    reserve: int,
    quota_window_consumption_known: bool | None = None,
    proven_quota_window_used: int | None = None,
    current_window_enumerated_tasks: int | None = None,
    unknown_quota_window_policy: str | None = None,
    hard_ceiling_reached: bool = False,
    # Compatibility aliases. New runtime code must use the quota-window names.
    lifetime_consumption_known: bool | None = None,
    proven_lifetime_used: int | None = None,
    current_enumerated_tasks: int | None = None,
    unknown_lifetime_policy: str | None = None,
) -> dict[str, Any]:
    """Evaluate task capacity for the *current provider quota window*.

    Historical/lifetime usage is never combined with current-window usage.
    Legacy arguments are accepted only as compatibility aliases for callers that
    already supply current-window evidence under the old names.
    """

    if ceiling < 0 or reserve < 0:
        raise ValueError("ceiling and reserve must be non-negative")
    if reserve > ceiling:
        raise ValueError("reserve cannot exceed ceiling")

    window_known = (
        bool(quota_window_consumption_known)
        if quota_window_consumption_known is not None
        else bool(lifetime_consumption_known)
    )
    proven_window_used = (
        proven_quota_window_used
        if proven_quota_window_used is not None
        else proven_lifetime_used
    )
    current_window = (
        current_window_enumerated_tasks
        if current_window_enumerated_tasks is not None
        else current_enumerated_tasks
    )
    policy = str(
        unknown_quota_window_policy
        if unknown_quota_window_policy is not None
        else unknown_lifetime_policy or "DENY"
    ).upper()

    if current_window is not None and current_window < 0:
        raise ValueError("current_window_enumerated_tasks must be non-negative")
    if proven_window_used is not None and proven_window_used < 0:
        raise ValueError("proven_quota_window_used must be non-negative")
    if policy not in UNKNOWN_QUOTA_WINDOW_POLICIES:
        raise ValueError(f"unknown unknown_quota_window_policy: {policy}")

    observed_lower_bound = max(
        int(current_window or 0),
        int(proven_window_used or 0),
    )
    effective_limit = max(0, ceiling - reserve)
    direct_limit_reached = bool(hard_ceiling_reached or observed_lower_bound >= effective_limit)

    common = {
        "schema_version": "3.0",
        "project": project,
        "ceiling": ceiling,
        "reserve": reserve,
        "budget_basis": "CURRENT_QUOTA_WINDOW",
        "quota_window_consumption_known": window_known,
        "proven_quota_window_used": proven_window_used,
        "current_window_enumerated_tasks": current_window,
        "observed_used_lower_bound": observed_lower_bound,
        "historical_usage_affects_capacity": False,
        "new_task_creation_authority": "PARENT_ONLY",
        "unknown_quota_window_policy": policy,
        # Compatibility output only; semantics are current-window, not lifetime.
        "unknown_lifetime_policy": policy,
        "current_enumeration_proves_lifetime_consumption": False,
        "hard_ceiling_reached": direct_limit_reached,
    }

    if direct_limit_reached:
        return {
            **common,
            "state": "DIRECT_CEILING_OR_RESERVE_BOUNDARY_REACHED",
            "safe_remaining": 0 if window_known else None,
            "observed_headroom": 0,
            "budget_allows_new_task": False,
            "automatic_new_task_creation": False,
            "fail_closed": True,
        }

    if not window_known:
        if policy == "DENY":
            return {
                **common,
                "state": "UNKNOWN_QUOTA_WINDOW_CONSUMPTION",
                "safe_remaining": None,
                "observed_headroom": max(0, effective_limit - observed_lower_bound),
                "budget_allows_new_task": False,
                "automatic_new_task_creation": False,
                "fail_closed": True,
            }

        return {
            **common,
            "state": "OWNER_POLICY_CAPACITY_AVAILABLE_WITH_UNKNOWN_QUOTA_WINDOW",
            "safe_remaining": None,
            "observed_headroom": max(0, effective_limit - observed_lower_bound),
            "budget_allows_new_task": True,
            "automatic_new_task_creation": False,
            "fail_closed": False,
        }

    if proven_window_used is None:
        raise ValueError("proven_quota_window_used is required when quota_window_consumption_known is true")

    if current_window is not None and current_window > proven_window_used:
        return {
            **common,
            "state": "TASK_BUDGET_EVIDENCE_INCONSISTENT",
            "safe_remaining": None,
            "observed_headroom": None,
            "budget_allows_new_task": False,
            "automatic_new_task_creation": False,
            "fail_closed": True,
        }

    safe_remaining = max(0, effective_limit - proven_window_used)
    state = "CAPACITY_AVAILABLE" if safe_remaining > 0 else "RESERVE_OR_CEILING_REACHED"
    return {
        **common,
        "state": state,
        "safe_remaining": safe_remaining,
        "observed_headroom": safe_remaining,
        "budget_allows_new_task": safe_remaining > 0,
        "automatic_new_task_creation": False,
        "fail_closed": safe_remaining <= 0,
    }


def evaluate_new_task_gate(
    budget: dict[str, Any],
    *,
    parent_gate_satisfied: bool,
    automatic_creation_authorized: bool = False,
) -> dict[str, Any]:
    budget_allows = bool(budget.get("budget_allows_new_task"))
    allowed = budget_allows and parent_gate_satisfied
    failures: list[str] = []
    if not budget_allows:
        failures.append("TASK_BUDGET_NOT_PROVEN_AVAILABLE")
    if not parent_gate_satisfied:
        failures.append("PARENT_GATE_REQUIRED")
    return {
        "schema_version": "3.0",
        "allowed": allowed,
        "authority": "PARENT_ONLY",
        "automatic_creation": bool(allowed and automatic_creation_authorized),
        "safe_remaining": budget.get("safe_remaining"),
        "observed_headroom": budget.get("observed_headroom"),
        "budget_state": budget.get("state"),
        "failures": failures,
    }
