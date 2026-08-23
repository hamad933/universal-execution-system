from __future__ import annotations

from typing import Any, Iterable


def _numeric(values: Iterable[Any]) -> list[float]:
    result: list[float] = []
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            result.append(number)
    return result


def _duration_summary(values: Iterable[Any]) -> dict[str, Any]:
    numbers = _numeric(values)
    if not numbers:
        return {"count": 0, "average_seconds": None, "max_seconds": None}
    return {
        "count": len(numbers),
        "average_seconds": sum(numbers) / len(numbers),
        "max_seconds": max(numbers),
    }


def build_operational_metrics(
    lanes: list[dict[str, Any]],
    *,
    task_budget: dict[str, Any],
) -> dict[str, Any]:
    waiting_ages = _numeric(lane.get("waiting_age_seconds") for lane in lanes)
    active_writer_count = sum(
        1 for lane in lanes if lane.get("active") and str(lane.get("role") or "").upper() == "WRITER"
    )
    active_reviewer_count = sum(
        1 for lane in lanes if lane.get("active") and str(lane.get("role") or "").upper() == "REVIEWER"
    )
    idle_lane_count = sum(1 for lane in lanes if lane.get("idle"))
    forgotten_lane_count = sum(1 for lane in lanes if lane.get("forgotten"))
    failed_session_count = sum(1 for lane in lanes if lane.get("terminal_failed_session"))
    unresolved_auto_safe_count = sum(
        1 for lane in lanes if lane.get("auto_safe_incident") and not lane.get("auto_safe_treated")
    )

    return {
        "schema_version": "2.0",
        "waiting_age": {
            "count": len(waiting_ages),
            "max_seconds": max(waiting_ages) if waiting_ages else None,
        },
        "time_to_route_review": _duration_summary(
            lane.get("time_to_route_review_seconds") for lane in lanes
        ),
        "time_to_correction": _duration_summary(
            lane.get("time_to_correction_seconds") for lane in lanes
        ),
        "idle_lane_count": idle_lane_count,
        "forgotten_lane_count": forgotten_lane_count,
        "failed_session_count": failed_session_count,
        "active_writer_count": active_writer_count,
        "active_reviewer_count": active_reviewer_count,
        "task_budget_state": task_budget.get("state", "UNKNOWN_LIFETIME_CONSUMPTION"),
        "unresolved_auto_safe_count": unresolved_auto_safe_count,
        "sanitized": True,
    }
