from __future__ import annotations

from typing import Any

DEFAULT_THRESHOLDS = {
    "waiting_seconds": 3600,
    "completed_review_unrouted_seconds": 1800,
    "unclassified_failure_seconds": 900,
    "heartbeat_seconds": 1800,
}


def normalize_thresholds(thresholds: dict[str, Any] | None = None) -> dict[str, float]:
    merged = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        merged.update(thresholds)
    normalized: dict[str, float] = {}
    for key in DEFAULT_THRESHOLDS:
        value = float(merged[key])
        if value < 0:
            raise ValueError(f"threshold {key} must be non-negative")
        normalized[key] = value
    return normalized


def evaluate_lane_watchdog(
    lane: dict[str, Any],
    *,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    limits = normalize_thresholds(thresholds)
    incidents: list[dict[str, Any]] = []
    lane_id = str(lane.get("lane_id") or lane.get("workstream_id") or "UNKNOWN")

    waiting_age = lane.get("waiting_age_seconds")
    if lane.get("waiting_class") and waiting_age is not None and float(waiting_age) > limits["waiting_seconds"]:
        incidents.append({"code": "WAITING_TOO_LONG", "severity": "INCIDENT"})

    review_age = lane.get("review_unrouted_age_seconds")
    if lane.get("review_completed") and not lane.get("review_routed"):
        if review_age is None or float(review_age) > limits["completed_review_unrouted_seconds"]:
            incidents.append({"code": "COMPLETED_REVIEW_NOT_ROUTED", "severity": "INCIDENT"})

    failure_age = lane.get("failure_unclassified_age_seconds")
    if lane.get("failed_state") and not lane.get("failure_classified"):
        if failure_age is None or float(failure_age) > limits["unclassified_failure_seconds"]:
            incidents.append({"code": "FAILED_STATE_UNCLASSIFIED", "severity": "INCIDENT"})

    heartbeat_age = lane.get("heartbeat_age_seconds")
    role = str(lane.get("role") or "").upper()
    if lane.get("active") and role in {"WRITER", "REVIEWER"}:
        if heartbeat_age is None or float(heartbeat_age) > limits["heartbeat_seconds"]:
            incidents.append({"code": "STALE_ACTIVE_HEARTBEAT", "severity": "WARNING"})

    if not lane.get("next_action") and not lane.get("stop_gate"):
        incidents.append({"code": "FORGOTTEN_LANE", "severity": "INCIDENT"})

    auto_safe_incident = bool(
        lane.get("auto_safe_incident")
        or (str(lane.get("authority") or "").upper() == "AUTO_SAFE" and incidents)
    )
    untreated_auto_safe = bool(auto_safe_incident and not lane.get("auto_safe_treated"))

    return {
        "schema_version": "2.0",
        "lane_id": lane_id,
        "incidents": incidents,
        "forgotten": any(item["code"] == "FORGOTTEN_LANE" for item in incidents),
        "untreated_auto_safe": untreated_auto_safe,
        "terminal_failed_session": bool(lane.get("terminal_failed_session")),
    }


def evaluate_control_cycle(
    lanes: list[dict[str, Any]],
    *,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evaluations = [evaluate_lane_watchdog(lane, thresholds=thresholds) for lane in lanes]
    untreated = [item["lane_id"] for item in evaluations if item["untreated_auto_safe"]]
    forgotten = [item["lane_id"] for item in evaluations if item["forgotten"]]
    failed_sessions = [item["lane_id"] for item in evaluations if item["terminal_failed_session"]]

    executable_lanes = []
    blocked_lanes = []
    for lane in lanes:
        lane_id = str(lane.get("lane_id") or lane.get("workstream_id") or "UNKNOWN")
        if lane.get("blocked"):
            blocked_lanes.append(lane_id)
        elif lane.get("next_action"):
            executable_lanes.append(lane_id)

    cycle_failed = bool(untreated)
    return {
        "schema_version": "2.0",
        "cycle_status": "CONTROL_CYCLE_FAILED" if cycle_failed else "CONTROL_CYCLE_OK",
        "lane_evaluations": evaluations,
        "unresolved_auto_safe_lanes": untreated,
        "forgotten_lanes": forgotten,
        "terminal_failed_sessions": failed_sessions,
        "executable_lanes": executable_lanes,
        "blocked_lanes": blocked_lanes,
        "blocked_lane_freezes_independent_lanes": False,
    }
