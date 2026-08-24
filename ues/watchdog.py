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
    "COMPLETED_OUTPUT_UNCONSUMED",
    "REVIEW_FINDINGS_UNROUTED",
    "CORRECTED_SHA_NOT_REREVIEWED",
    "STALE_EXACT_SHA_EVIDENCE",
    "DUPLICATE_ACTIVE_LINEAGE",
    "ADAPTER_AUTHORITY_DRIFT",
    "PROVIDER_BINDING_DRIFT",
    "WAITING_CONTROLLER_RESOLVABLE",
    "NOOP_GENERATION",
    "TRIGGERABLE_CI_NOT_TRIGGERED",
    "ROUTE_PROFILE_NOT_EXERCISED",
    "OBSOLETE_TECHNICAL_TRUTH",
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

RECOVERY_ACTIONS = {
    "WAITING_UNRESOLVED": "RECONCILE_WAITING_AUTHORITY_AND_ACTIVITY",
    "COMPLETED_REVIEW_NOT_ROUTED": "CONSUME_REVIEW_AND_ROUTE_EXACT_SHA_VERDICT",
    "FAILED_STATE_UNCLASSIFIED": "CLASSIFY_FAILURE_FROM_DIRECT_PROVIDER_STATE",
    "STALE_ACTIVE_HEARTBEAT": "RECONCILE_PROVIDER_BEFORE_REPLACEMENT",
    "FORGOTTEN_LANE": "RECONSTRUCT_LANE_AND_SELECT_SAFE_NEXT_ACTION",
    "CORRECTION_REREVIEW_LOOP_STALLED": "ROUTE_CORRECTED_SHA_TO_PERSISTENT_REVIEWER",
    "EXACT_HEAD_EVIDENCE_DRIFT_UNRESOLVED": "INVALIDATE_STALE_EVIDENCE_AND_REVERIFY_EXACT_HEAD",
    "REUSE_CRITICAL_PATH_DRAG": "PROVE_REUSE_NONVIABLE_THEN_GUARDED_NEXT_GENERATION",
    "COMPLETED_OUTPUT_UNCONSUMED": "VERIFY_CANDIDATE_SHA_AND_ROUTE_COMPLETED_OUTPUT_NOW",
    "REVIEW_FINDINGS_UNROUTED": "ROUTE_EXACT_SHA_FINDINGS_TO_SAME_WRITER_LINEAGE",
    "CORRECTED_SHA_NOT_REREVIEWED": "INVALIDATE_STALE_REVIEW_AND_REREVIEW_CORRECTED_SHA",
    "STALE_EXACT_SHA_EVIDENCE": "INVALIDATE_AND_TRIGGER_EXACT_HEAD_EVIDENCE",
    "DUPLICATE_ACTIVE_LINEAGE": "RECONCILE_DUPLICATE_AND_BLOCK_NEW_EFFECT",
    "ADAPTER_AUTHORITY_DRIFT": "RESOLVE_CURRENT_GOVERNED_AUTHORITY_IGNORE_STALE_SNAPSHOT",
    "PROVIDER_BINDING_DRIFT": "RECONCILE_PROVIDER_BINDING_BEFORE_EFFECT",
    "WAITING_CONTROLLER_RESOLVABLE": "ANSWER_ONCE_WITH_DURABLE_IDEMPOTENCY_AND_CONTINUE",
    "NOOP_GENERATION": "PROVE_REPEATED_INEFFECTIVENESS_THEN_GUARDED_NEXT_GENERATION",
    "TRIGGERABLE_CI_NOT_TRIGGERED": "TRIGGER_EXACT_HEAD_CI_OR_EVIDENCE",
    "ROUTE_PROFILE_NOT_EXERCISED": "BOUNDED_WORKFLOW_DISPATCH_EXACT_PROFILE",
    "OBSOLETE_TECHNICAL_TRUTH": "REFRESH_GITHUB_TRUTH_AND_RECONCILE_GOVERNED_STATE",
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
        "schema_version": "2.4",
        "thresholds": thresholds,
        "enabled_categories": enabled_categories,
        "source": "ADAPTER_OR_CONTROLLER_POLICY" if policy else "UNIVERSAL_DEFAULT_POLICY",
    }


def _over_threshold(age: Any, limit: float, *, missing_is_incident: bool = True) -> bool:
    if age is None:
        return missing_is_incident
    return float(age) > limit


def _incident(code: str, severity: str = "INCIDENT") -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "recommended_action": RECOVERY_ACTIONS[code],
        "recommend_wait_only": False,
    }


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
            incidents.append(_incident("WAITING_UNRESOLVED"))

    if "COMPLETED_REVIEW_NOT_ROUTED" in enabled and lane.get("review_completed") and not lane.get("review_routed"):
        if _over_threshold(lane.get("review_unrouted_age_seconds"), limits["completed_review_unrouted_seconds"]):
            incidents.append(_incident("COMPLETED_REVIEW_NOT_ROUTED"))

    if "FAILED_STATE_UNCLASSIFIED" in enabled and lane.get("failed_state") and not lane.get("failure_classified"):
        if _over_threshold(lane.get("failure_unclassified_age_seconds"), limits["unclassified_failure_seconds"]):
            incidents.append(_incident("FAILED_STATE_UNCLASSIFIED"))

    role = str(lane.get("role") or "").upper()
    if "STALE_ACTIVE_HEARTBEAT" in enabled and lane.get("active") and role in {"WRITER", "REVIEWER"}:
        if _over_threshold(lane.get("heartbeat_age_seconds"), limits["heartbeat_seconds"]):
            incidents.append(_incident("STALE_ACTIVE_HEARTBEAT", "WARNING"))

    loop_pending = bool(lane.get("correction_pending") or lane.get("rereview_pending"))
    if "CORRECTION_REREVIEW_LOOP_STALLED" in enabled and loop_pending:
        if _over_threshold(lane.get("correction_rereview_age_seconds"), limits["correction_rereview_stalled_seconds"]):
            incidents.append(_incident("CORRECTION_REREVIEW_LOOP_STALLED"))

    if "EXACT_HEAD_EVIDENCE_DRIFT_UNRESOLVED" in enabled and lane.get("evidence_drift_unresolved"):
        if _over_threshold(lane.get("evidence_drift_age_seconds"), limits["evidence_drift_unresolved_seconds"]):
            incidents.append(_incident("EXACT_HEAD_EVIDENCE_DRIFT_UNRESOLVED"))

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
            incidents.append(_incident("REUSE_CRITICAL_PATH_DRAG"))

    direct_rules = (
        ("COMPLETED_OUTPUT_UNCONSUMED", "completed_output_unconsumed"),
        ("REVIEW_FINDINGS_UNROUTED", "review_findings_unrouted"),
        ("CORRECTED_SHA_NOT_REREVIEWED", "corrected_sha_not_rereviewed"),
        ("STALE_EXACT_SHA_EVIDENCE", "stale_exact_sha_evidence"),
        ("DUPLICATE_ACTIVE_LINEAGE", "duplicate_active_lineage"),
        ("ADAPTER_AUTHORITY_DRIFT", "adapter_authority_drift"),
        ("PROVIDER_BINDING_DRIFT", "provider_binding_drift"),
        ("WAITING_CONTROLLER_RESOLVABLE", "waiting_controller_resolvable"),
        ("NOOP_GENERATION", "repeated_proven_noop"),
        ("TRIGGERABLE_CI_NOT_TRIGGERED", "triggerable_ci_not_triggered"),
        ("ROUTE_PROFILE_NOT_EXERCISED", "route_profile_not_exercised"),
        ("OBSOLETE_TECHNICAL_TRUTH", "obsolete_technical_truth"),
    )
    for code, flag in direct_rules:
        if code in enabled and lane.get(flag):
            incidents.append(_incident(code))

    if "FORGOTTEN_LANE" in enabled and not lane.get("next_action") and not lane.get("stop_gate"):
        incidents.append(_incident("FORGOTTEN_LANE"))

    proven_auto_safe_incident = bool(lane.get("auto_safe_incident_proven") or lane.get("auto_safe_incident"))
    untreated_auto_safe = bool(proven_auto_safe_incident and not lane.get("auto_safe_treated"))
    recommended_actions = [item["recommended_action"] for item in incidents]

    return {
        "schema_version": "2.4",
        "lane_id": lane_id,
        "incidents": incidents,
        "recommended_actions": recommended_actions,
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
        "schema_version": "2.4",
        "cycle_status": "CONTROL_CYCLE_FAILED" if cycle_failed else "CONTROL_CYCLE_OK",
        "lane_evaluations": evaluations,
        "unresolved_auto_safe_lanes": untreated,
        "forgotten_lanes": forgotten,
        "terminal_failed_sessions": failed_sessions,
        "executable_lanes": executable_lanes,
        "blocked_lanes": blocked_lanes,
        "blocked_lane_freezes_independent_lanes": False,
    }
