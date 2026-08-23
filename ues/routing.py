from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

WAITING_CLASSES = {
    "POLICY_RESOLVABLE",
    "ENVIRONMENT_MISMATCH",
    "CI_DEPENDENT",
    "REVIEW_DEPENDENT",
    "TOOL_LIMIT",
    "SHARED_CONTRACT_REQUIRED",
    "SCOPE_OR_NEW_TASK_REQUIRED",
    "OWNER_DECISION_REQUIRED",
    "UNCLASSIFIED",
}

WAITING_AUTHORITY = {
    "POLICY_RESOLVABLE": "AUTO_SAFE",
    "ENVIRONMENT_MISMATCH": "AUTO_SAFE",
    "CI_DEPENDENT": "AUTO_SAFE",
    "REVIEW_DEPENDENT": "AUTO_SAFE",
    "TOOL_LIMIT": "AUTO_SAFE",
    "SHARED_CONTRACT_REQUIRED": "PARENT_REQUIRED",
    "SCOPE_OR_NEW_TASK_REQUIRED": "PARENT_REQUIRED",
    "OWNER_DECISION_REQUIRED": "OWNER_REQUIRED",
    "UNCLASSIFIED": "DENY",
}

_AUTO_SAFE_PREDICATES = {
    "POLICY_RESOLVABLE": "project_policy_permits",
    "ENVIRONMENT_MISMATCH": "bounded_workaround_authorized",
    "CI_DEPENDENT": "deterministic_evidence",
    "REVIEW_DEPENDENT": "deterministic_evidence",
    "TOOL_LIMIT": "bounded_no_scope_expansion",
}


def waiting_routing_table() -> dict[str, str]:
    return dict(WAITING_AUTHORITY)


def route_waiting(
    waiting_class: str,
    *,
    exact_state_read: bool,
    latest_activity_read: bool,
    same_session_available: bool = True,
    project_policy_permits: bool = False,
    bounded_workaround_authorized: bool = False,
    deterministic_evidence: bool = False,
    bounded_no_scope_expansion: bool = False,
) -> dict[str, Any]:
    normalized = str(waiting_class or "UNCLASSIFIED").upper()
    if normalized not in WAITING_CLASSES:
        normalized = "UNCLASSIFIED"

    authority = WAITING_AUTHORITY[normalized]
    reasons: list[str] = []
    action = "STOP"

    if not exact_state_read or not latest_activity_read:
        authority = "DENY"
        reasons.append("EXACT_PROVIDER_STATE_AND_LATEST_ACTIVITY_REQUIRED")
    elif authority == "AUTO_SAFE":
        predicate_name = _AUTO_SAFE_PREDICATES[normalized]
        predicates = {
            "project_policy_permits": project_policy_permits,
            "bounded_workaround_authorized": bounded_workaround_authorized,
            "deterministic_evidence": deterministic_evidence,
            "bounded_no_scope_expansion": bounded_no_scope_expansion,
        }
        if not predicates[predicate_name]:
            authority = "DENY"
            reasons.append(f"AUTO_SAFE_PRECONDITION_MISSING:{predicate_name}")
        elif not same_session_available:
            authority = "DENY"
            reasons.append("SAME_SESSION_CONTINUATION_UNAVAILABLE")
        else:
            action = "CONTINUE_SAME_SESSION"
            reasons.append("BOUNDED_SAME_SESSION_CONTINUATION")
    elif authority == "PARENT_REQUIRED":
        action = "ESCALATE_PARENT"
        reasons.append("PARENT_AUTHORITY_REQUIRED")
    elif authority == "OWNER_REQUIRED":
        action = "ESCALATE_OWNER"
        reasons.append("OWNER_DECISION_REQUIRED")
    else:
        reasons.append("UNCLASSIFIED_FAIL_CLOSED")

    return {
        "schema_version": "2.0",
        "waiting_class": normalized,
        "authority": authority,
        "action": action,
        "same_session_preferred": True,
        "new_task_justified_by_waiting": False,
        "automatic_new_task_creation": False,
        "reasons": reasons,
    }


def _group_findings(findings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        root_cause = str(finding.get("root_cause") or finding.get("category") or "UNCLASSIFIED")
        grouped[root_cause].append(
            {
                "id": finding.get("id"),
                "summary": finding.get("summary"),
                "paths": sorted(str(path) for path in (finding.get("paths") or [])),
            }
        )
    return [
        {"root_cause": root_cause, "findings": grouped[root_cause]}
        for root_cause in sorted(grouped)
    ]


def route_reviewer_to_writer(
    *,
    workstream_id: str,
    reviewed_sha: str,
    candidate_sha: str,
    reviewer_role_valid: bool,
    reviewer_independent: bool,
    reviewer_mutation_detected: bool,
    reviewer_mutation_adjudicated: bool,
    reviewer_mutation_disqualifying: bool,
    writer_binding_proven: bool,
    finding_within_writer_scope: bool,
    correction_in_flight: bool,
    correction_already_sent: bool,
    findings: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    failures: list[str] = []
    if not reviewed_sha or reviewed_sha != candidate_sha:
        failures.append("REVIEWED_SHA_MISMATCH")
    if not reviewer_role_valid:
        failures.append("REVIEWER_ROLE_INVALID")
    if not reviewer_independent:
        failures.append("REVIEWER_INDEPENDENCE_INVALID")
    if reviewer_mutation_detected and not reviewer_mutation_adjudicated:
        failures.append("REVIEWER_MUTATION_UNADJUDICATED")
    if reviewer_mutation_detected and reviewer_mutation_adjudicated and reviewer_mutation_disqualifying:
        failures.append("REVIEWER_MUTATION_DISQUALIFYING")
    if not writer_binding_proven:
        failures.append("WRITER_BINDING_UNPROVEN")
    if not finding_within_writer_scope:
        failures.append("FINDING_OUTSIDE_WRITER_SCOPE")
    if correction_in_flight or correction_already_sent:
        failures.append("DUPLICATE_OR_IN_FLIGHT_CORRECTION")

    packet = _group_findings(findings)
    if not packet:
        failures.append("NO_ROUTABLE_FINDINGS")

    routable = not failures
    return {
        "schema_version": "2.0",
        "workstream_id": workstream_id,
        "reviewed_sha": reviewed_sha,
        "candidate_sha": candidate_sha,
        "authority": "AUTO_SAFE" if routable else "DENY",
        "action": "SEND_ONE_CORRECTION_PACKET_TO_EXISTING_WRITER" if routable else "STOP",
        "correction_operation_key": f"correction:{workstream_id}:{candidate_sha}" if candidate_sha else None,
        "correction_packet": packet if routable else [],
        "grouped_packet_count": 1 if routable else 0,
        "reuse_existing_writer": routable,
        "automatic_new_task_creation": False,
        "failures": failures,
    }


def route_writer_to_reviewer(
    *,
    prior_reviewed_sha: str | None,
    new_candidate_sha: str,
    ci_evidence_sha: str | None,
    review_evidence_sha: str | None,
    existing_reviewer_available: bool,
    existing_reviewer_safe_to_reuse: bool,
    new_reviewer_policy_allows: bool,
    parent_gate_satisfied: bool,
) -> dict[str, Any]:
    prior_review_stale = bool(prior_reviewed_sha and prior_reviewed_sha != new_candidate_sha)
    evidence_exact = bool(
        new_candidate_sha
        and ci_evidence_sha == new_candidate_sha
        and review_evidence_sha == new_candidate_sha
    )

    failures: list[str] = []
    if not evidence_exact:
        failures.append("EXACT_NEW_SHA_EVIDENCE_REQUIRED")

    authority = "DENY"
    action = "STOP"
    if evidence_exact and existing_reviewer_available and existing_reviewer_safe_to_reuse:
        authority = "AUTO_SAFE"
        action = "ROUTE_TO_EXISTING_REVIEWER"
    elif evidence_exact and new_reviewer_policy_allows and parent_gate_satisfied:
        authority = "PARENT_REQUIRED"
        action = "PARENT_MAY_CREATE_NEW_REVIEWER"
    elif evidence_exact and not existing_reviewer_available:
        failures.append("NO_SAFE_EXISTING_REVIEWER")
        if not new_reviewer_policy_allows:
            failures.append("NEW_REVIEWER_POLICY_DISALLOWS")
        if not parent_gate_satisfied:
            failures.append("PARENT_GATE_NOT_SATISFIED")
    elif evidence_exact and existing_reviewer_available and not existing_reviewer_safe_to_reuse:
        failures.append("EXISTING_REVIEWER_REUSE_UNSAFE")

    return {
        "schema_version": "2.0",
        "prior_review_stale": prior_review_stale,
        "new_candidate_sha": new_candidate_sha,
        "exact_new_sha_evidence": evidence_exact,
        "authority": authority,
        "action": action,
        "reuse_existing_reviewer": action == "ROUTE_TO_EXISTING_REVIEWER",
        "automatic_new_reviewer_creation": False,
        "new_task_creation_authority": "PARENT_ONLY",
        "failures": failures,
    }


def route_terminal_session_failure(*, same_session_available: bool) -> dict[str, Any]:
    if same_session_available:
        return {
            "schema_version": "2.0",
            "classification": "RECOVER_SAME_LINEAGE",
            "authority": "AUTO_SAFE",
            "action": "CONTINUE_SAME_SESSION",
            "automatic_new_task_creation": False,
        }
    return {
        "schema_version": "2.0",
        "classification": "SESSION_CONTINUATION_UNAVAILABLE",
        "authority": "PARENT_REQUIRED",
        "action": "NEW_TASK_RECOMMENDED",
        "automatic_new_task_creation": False,
        "new_task_creation_authority": "PARENT_ONLY",
    }
