from __future__ import annotations

from typing import Any

WATCHDOG_CATEGORIES = {
    "WAITING_UNRESOLVED",
    "COMPLETED_REVIEW_NOT_ROUTED",
    "FAILED_STATE_UNCLASSIFIED",
    "STALE_ACTIVE_HEARTBEAT",
    "FORGOTTEN_LANE",
    "CORRECTION_REREVIEW_LOOP_STALLED",
    "EXACT_HEAD_EVIDENCE_DRIFT_UNRESOLVED",
    "REUSE_CRITICAL_PATH_DRAG",
}

DEFAULT_THRESHOLDS = {
    "waiting_unresolved_seconds": 3600,
    "completed_review_unrouted_seconds": 1800,
    "unclassified_failure_seconds": 900,
    "heartbeat_seconds": 1800,
    "correction_rereview_stalled_seconds": 3600,
    "evidence_drift_unresolved_seconds": 1800,
    "reuse_critical_path_drag_seconds": 900,
}


def normalize_watchdog_policy(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = dict(policy or {})
    merged = dict(DEFAULT_THRESHOLDS)
    merged.update(policy.get("thresholds") or {})
    thresholds: dict[str, float] = {}
    for key in DEFAULT_THRESHOLDS:
        value = float(merged[key])
        if value < 0:
            raise ValueError(f"threshold {key} must be non-negative")
        thresholds[key] = value

    configured_categories = policy.get("enabled_categories")
    if configured_categories is None:
        enabled_categories = set(WATCHDOG_CATEGORIES)
    else:
        enabled_categories = {str(item).upper() for item in configured_categories}
        unknown = enabled_categories - WATCHDOG_CATEGORIES
        if unknown:
            raise ValueError(f"unknown watchdog categories: {sorted(unknown)}")

    return {
        "schema_version": "2.3",
        "thresholds": thresholds,
        "enabled_categories": enabled_categories,
        "source": "ADAPTER_OR_CONTROLLER_POLICY" if policy else "UNIVERSAL_DEFAULT_POLICY",
    }


def _over_threshold(age: Any, limit: float, *, missing_is_incident: bool = True) -> bool:
    if age is None:
        return missing_is_incident
    return float(age) > limit


def evaluate_lane_watchdog(
    lane: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_policy = normalize_watchdog_policy(policy)
    limits = normalized_policy["thresholds"]
    enabled = normalized_policy["enabled_categories"]
    incidents: list[dict[str, Any]] = []
    lane_id = str(lane.get("lane_id") or lane.get("workstream_id") or "UNKNOWN")

    if "WAITING_UNRESOLVED" in enabled and lane.get("waiting_class") and not lane.get("waiting_resolved"):
        if _over_threshold(lane.get("waiting_age_seconds"), limits["waiting_unresolved_seconds"]):
            incidents.append({"code": "WAITING_UNRESOLVED", "severity": "INCIDENT"})

    if "COMPLETED_REVIEW_NOT_ROUTED" in enabled and lane.get("review_completed") and not lane.get("review_routed"):
        if _over_threshold(lane.get("review_unrouted_age_seconds"), limits["completed_review_unrouted_seconds"]):
            incidents.append({"code": "COMPLETED_REVIEW_NOT_ROUTED", "severity": "INCIDENT"})

    if "FAILED_STATE_UNCLASSIFIED" in enabled and lane.get("failed_state") and not lane.get("failure_classified"):
        if _over_threshold(lane.get("failure_unclassified_age_seconds"), limits["unclassified_failure_seconds"]):
            incidents.append({"code": "FAILED_STATE_UNCLASSIFIED", "severity": "INCIDENT"})

    role = str(lane.get("role") or "").upper()
    if "STALE_ACTIVE_HEARTBEAT" in enabled and lane.get("active") and role in {"WRITER", "REVIEWER"}:
        if _over_threshold(lane.get("heartbeat_age_seconds"), limits["heartbeat_seconds"]):
            incidents.append({"code": "STALE_ACTIVE_HEARTBEAT", "severity": "WARNING"})

    loop_pending = bool(lane.get("correction_pending") or lane.get("rereview_pending"))
    if "CORRECTION_REREVIEW_LOOP_STALLED" in enabled and loop_pending:
        if _over_threshold(lane.get("correction_rereview_age_seconds"), limits["correction_rereview_stalled_seconds"]):
            incidents.append({"code": "CORRECTION_REREVIEW_LOOP_STALLED", "severity": "INCIDENT"})

    if "EXACT_HEAD_EVIDENCE_DRIFT_UNRESOLVED" in enabled and lane.get("evidence_drift_unresolved"):
        if _over_threshold(lane.get("evidence_drift_age_seconds"), limits["evidence_drift_unresolved_seconds"]):
            incidents.append({"code": "EXACT_HEAD_EVIDENCE_DRIFT_UNRESOLVED", "severity": "INCIDENT"})

    reuse_nonviable = bool(lane.get("reuse_attempts_exhausted") or lane.get("reuse_viable") is False)
    if (
        "REUSE_CRITICAL_PATH_DRAG" in enabled
        and lane.get("reuse_path_selected")
        and reuse_nonviable
        and lane.get("replacement_ready")
    ):
        if _over_threshold(
            lane.get("reuse_delay_age_seconds"),
            limits["reuse_critical_path_drag_seconds"],
            missing_is_incident=False,
        ):
            incidents.append({"code": "REUSE_CRITICAL_PATH_DRAG", "severity": "INCIDENT"})

    if "FORGOTTEN_LANE" in enabled and not lane.get("next_action") and not lane.get("stop_gate"):
        incidents.append({"code": "FORGOTTEN_LANE", "severity": "INCIDENT"})

    proven_auto_safe_incident = bool(
        lane.get("auto_safe_incident_proven") or lane.get("auto_safe_incident")
    )
    untreated_auto_safe = bool(proven_auto_safe_incident and not lane.get("auto_safe_treated"))

    return {
        "schema_version": "2.3",
        "lane_id": lane_id,
        "incidents": incidents,
        "forgotten": any(item["code"] == "FORGOTTEN_LANE" for item in incidents),
        "proven_auto_safe_incident": proven_auto_safe_incident,
        "untreated_auto_safe": untreated_auto_safe,
        "terminal_failed_session": bool(lane.get("terminal_failed_session")),
        "watchdog_policy_source": normalized_policy["source"],
    }


def evaluate_control_cycle(
    lanes: list[dict[str, Any]],
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evaluations = [evaluate_lane_watchdog(lane, policy=policy) for lane in lanes]
    untreated = [item["lane_id"] for item in evaluations if item["untreated_auto_safe"]]
    forgotten = [item["lane_id"] for item in evaluations if item["forgotten"]]
    failed_sessions = [item["lane_id"] for item in evaluations if item["terminal_failed_session"]]

    executable_lanes: list[str] = []
    blocked_lanes: list[str] = []
    for lane in lanes:
        lane_id = str(lane.get("lane_id") or lane.get("workstream_id") or "UNKNOWN")
        if lane.get("blocked") or lane.get("stop_gate"):
            blocked_lanes.append(lane_id)
        elif lane.get("next_action"):
            executable_lanes.append(lane_id)

    cycle_failed = bool(untreated or forgotten)
    return {
        "schema_version": "2.3",
        "cycle_status": "CONTROL_CYCLE_FAILED" if cycle_failed else "CONTROL_CYCLE_OK",
        "lane_evaluations": evaluations,
        "unresolved_auto_safe_lanes": untreated,
        "forgotten_lanes": forgotten,
        "terminal_failed_sessions": failed_sessions,
        "executable_lanes": executable_lanes,
        "blocked_lanes": blocked_lanes,
        "blocked_lane_freezes_independent_lanes": False,
    }
