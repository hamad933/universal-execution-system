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

# This table expresses generic capability/authority class only. For every
# mutation-capable AUTO_SAFE class, project adapter authorization remains a
# separate mandatory gate in route_waiting().
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

_PROVEN_WRITER_BINDING_KINDS = {"EXPLICIT", "DIRECT", "CANONICAL"}


def waiting_routing_table() -> dict[str, str]:
    """Return generic waiting-class authority capabilities.

    AUTO_SAFE here means only "eligible for project authorization"; it never
    grants mutation authority by itself.
    """

    return dict(WAITING_AUTHORITY)


def _normalize_allowlist(project_auto_safe_allowlist: Iterable[str] | None) -> set[str] | None:
    if project_auto_safe_allowlist is None:
        return None
    return {str(item).upper() for item in project_auto_safe_allowlist}


def route_waiting(
    waiting_class: str,
    *,
    exact_state_read: bool,
    latest_activity_read: bool,
    continuation_binding_proven: bool,
    project_auto_safe_allowlist: Iterable[str] | None,
    same_session_available: bool = True,
    project_policy_permits: bool = False,
    bounded_workaround_authorized: bool = False,
    deterministic_evidence: bool = False,
    bounded_no_scope_expansion: bool = False,
) -> dict[str, Any]:
    normalized = str(waiting_class or "UNCLASSIFIED").upper()
    if normalized not in WAITING_CLASSES:
        normalized = "UNCLASSIFIED"

    generic_authority = WAITING_AUTHORITY[normalized]
    authority = generic_authority
    reasons: list[str] = []
    action = "STOP"
    allowlist = _normalize_allowlist(project_auto_safe_allowlist)

    if not exact_state_read or not latest_activity_read:
        authority = "DENY"
        reasons.append("EXACT_PROVIDER_STATE_AND_LATEST_ACTIVITY_REQUIRED")
    elif generic_authority == "AUTO_SAFE":
        if allowlist is None:
            authority = "DENY"
            reasons.append("PROJECT_AUTO_SAFE_ALLOWLIST_REQUIRED")
        elif normalized not in allowlist:
            authority = "PARENT_REQUIRED"
            action = "ESCALATE_PARENT"
            reasons.append("WAITING_CLASS_NOT_PROJECT_AUTO_SAFE_AUTHORIZED")
        elif not continuation_binding_proven:
            authority = "DENY"
            reasons.append("CONTINUATION_BINDING_UNPROVEN")
        else:
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
                authority = "PARENT_REQUIRED"
                action = "PARENT_CONTINUATION_OR_NEW_TASK_RECOMMENDATION"
                reasons.append("SAME_SESSION_CONTINUATION_UNAVAILABLE")
            else:
                authority = "AUTO_SAFE"
                action = "CONTINUE_SAME_SESSION"
                reasons.append("PROJECT_AUTHORIZED_BOUNDED_SAME_SESSION_CONTINUATION")
    elif generic_authority == "PARENT_REQUIRED":
        action = "ESCALATE_PARENT"
        reasons.append("PARENT_AUTHORITY_REQUIRED")
    elif generic_authority == "OWNER_REQUIRED":
        action = "ESCALATE_OWNER"
        reasons.append("OWNER_DECISION_REQUIRED")
    else:
        reasons.append("UNCLASSIFIED_FAIL_CLOSED")

    return {
        "schema_version": "2.1",
        "waiting_class": normalized,
        "generic_authority": generic_authority,
        "authority": authority,
        "action": action,
        "project_auto_safe_authorized": bool(allowlist is not None and normalized in allowlist),
        "same_session_preferred": True,
        "new_task_justified_by_waiting": False,
        "automatic_new_task_creation": False,
        "new_task_creation_authority": "PARENT_ONLY",
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
    result: list[dict[str, Any]] = []
    for root_cause in sorted(grouped):
        entries = sorted(grouped[root_cause], key=lambda item: str(item.get("id") or ""))
        result.append({"root_cause": root_cause, "findings": entries})
    return result


def _finding_effect_identity(packet: list[dict[str, Any]]) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    for group in packet:
        for finding in group["findings"]:
            effects.append(
                {
                    "finding_id": finding.get("id"),
                    "root_cause": group["root_cause"],
                    "paths": list(finding.get("paths") or []),
                }
            )
    return effects


def _operation_identity_input(
    *,
    project: str,
    workstream_id: str,
    effect_type: str,
    writer_session_id: str | None,
    reviewer_session_id: str | None,
    candidate_sha: str | None,
    reviewed_sha: str | None,
    effect_identity: Any,
) -> dict[str, Any]:
    """Return semantic identity fields for Domain D/Integration canonicalization.

    Domain C deliberately does not hash, concatenate, or otherwise mint an
    operation key. Domain D/Integration owns canonical operation identity and
    durable idempotency state.
    """

    return {
        "schema_version": "2.1",
        "contract": "CANONICAL_OPERATION_IDENTITY_INPUT",
        "identity_owner": "DOMAIN_D_OR_INTEGRATION",
        "action": effect_type,
        "project": project,
        "workstream_id": workstream_id,
        "identity": {
            "writer_session_id": writer_session_id,
            "reviewer_session_id": reviewer_session_id,
            "candidate_sha": candidate_sha,
            "reviewed_sha": reviewed_sha,
            "effect_identity": effect_identity,
        },
    }


def route_reviewer_to_writer(
    *,
    project: str,
    workstream_id: str,
    writer_session_id: str | None,
    reviewer_session_id: str | None,
    reviewed_sha: str,
    candidate_sha: str,
    reviewer_role_valid: bool,
    reviewer_independent: bool,
    reviewer_mutation_detected: bool,
    reviewer_mutation_adjudicated: bool,
    reviewer_mutation_disqualifying: bool,
    writer_binding_proven: bool,
    writer_binding_kind: str,
    finding_within_writer_scope: bool,
    canonical_operation_active: bool,
    canonical_operation_confirmed: bool,
    findings: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    failures: list[str] = []
    if not project:
        failures.append("PROJECT_IDENTITY_REQUIRED")
    if not workstream_id:
        failures.append("WORKSTREAM_IDENTITY_REQUIRED")
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
    if not reviewer_session_id:
        failures.append("REVIEWER_SESSION_REQUIRED")
    binding_kind = str(writer_binding_kind or "UNPROVEN").upper()
    if not writer_binding_proven or binding_kind not in _PROVEN_WRITER_BINDING_KINDS or not writer_session_id:
        failures.append("WRITER_BINDING_UNPROVEN")
    if not finding_within_writer_scope:
        failures.append("FINDING_OUTSIDE_WRITER_SCOPE")
    if canonical_operation_active or canonical_operation_confirmed:
        failures.append("DUPLICATE_OR_IN_FLIGHT_CORRECTION")

    packet = _group_findings(findings)
    if not packet:
        failures.append("NO_ROUTABLE_FINDINGS")
    elif any(finding.get("id") in (None, "") for group in packet for finding in group["findings"]):
        failures.append("FINDING_EFFECT_IDENTITY_REQUIRED")

    operation_identity_input = _operation_identity_input(
        project=project,
        workstream_id=workstream_id,
        effect_type="REVIEW_CORRECTION_PACKET",
        writer_session_id=writer_session_id,
        reviewer_session_id=reviewer_session_id,
        candidate_sha=candidate_sha,
        reviewed_sha=reviewed_sha,
        effect_identity=_finding_effect_identity(packet),
    )

    routable = not failures
    return {
        "schema_version": "2.1",
        "project": project,
        "workstream_id": workstream_id,
        "reviewed_sha": reviewed_sha,
        "candidate_sha": candidate_sha,
        "authority": "AUTO_SAFE" if routable else "DENY",
        "action": "SEND_ONE_CORRECTION_PACKET_TO_EXISTING_WRITER" if routable else "STOP",
        "operation_identity_input": operation_identity_input,
        "correction_packet": packet if routable else [],
        "grouped_packet_count": 1 if routable else 0,
        "reuse_existing_writer": routable,
        "automatic_new_task_creation": False,
        "failures": failures,
    }


def route_writer_to_reviewer(
    *,
    project: str,
    workstream_id: str,
    writer_session_id: str | None,
    reviewer_session_id: str | None,
    prior_reviewed_sha: str | None,
    new_candidate_sha: str,
    ci_evidence_sha: str | None,
    required_ci_proven: bool,
    existing_reviewer_available: bool,
    existing_reviewer_binding_proven: bool,
    existing_reviewer_safe_to_reuse: bool,
    new_reviewer_policy_allows: bool,
    parent_gate_satisfied: bool,
) -> dict[str, Any]:
    prior_review_stale = bool(prior_reviewed_sha and prior_reviewed_sha != new_candidate_sha)
    exact_new_sha_ci = bool(
        new_candidate_sha
        and required_ci_proven
        and ci_evidence_sha == new_candidate_sha
    )

    failures: list[str] = []
    if not project:
        failures.append("PROJECT_IDENTITY_REQUIRED")
    if not workstream_id:
        failures.append("WORKSTREAM_IDENTITY_REQUIRED")
    if not exact_new_sha_ci:
        failures.append("EXACT_REQUIRED_CI_FOR_NEW_SHA_REQUIRED")

    authority = "DENY"
    action = "STOP"
    reusable_reviewer = bool(
        exact_new_sha_ci
        and existing_reviewer_available
        and existing_reviewer_binding_proven
        and existing_reviewer_safe_to_reuse
        and reviewer_session_id
    )

    if reusable_reviewer and not failures:
        authority = "AUTO_SAFE"
        action = "DISPATCH_RE_REVIEW_TO_EXISTING_REVIEWER"
    elif exact_new_sha_ci and existing_reviewer_available:
        if not existing_reviewer_binding_proven or not reviewer_session_id:
            failures.append("EXISTING_REVIEWER_BINDING_UNPROVEN")
        if not existing_reviewer_safe_to_reuse:
            failures.append("EXISTING_REVIEWER_REUSE_UNSAFE")
    elif exact_new_sha_ci and not existing_reviewer_available:
        if new_reviewer_policy_allows and parent_gate_satisfied:
            authority = "PARENT_REQUIRED"
            action = "PARENT_MAY_CREATE_NEW_REVIEWER"
        else:
            failures.append("NO_SAFE_EXISTING_REVIEWER")
            if not new_reviewer_policy_allows:
                failures.append("NEW_REVIEWER_POLICY_DISALLOWS")
            if not parent_gate_satisfied:
                failures.append("PARENT_GATE_NOT_SATISFIED")

    operation_identity_input = _operation_identity_input(
        project=project,
        workstream_id=workstream_id,
        effect_type="RE_REVIEW_DISPATCH",
        writer_session_id=writer_session_id,
        reviewer_session_id=reviewer_session_id,
        candidate_sha=new_candidate_sha,
        reviewed_sha=prior_reviewed_sha,
        effect_identity={"reason": "CANDIDATE_SHA_MOVED", "prior_review_stale": prior_review_stale},
    )

    return {
        "schema_version": "2.1",
        "prior_review_stale": prior_review_stale,
        "new_candidate_sha": new_candidate_sha,
        "exact_required_ci_for_new_sha": exact_new_sha_ci,
        "pre_dispatch_review_evidence_required": False,
        "authority": authority,
        "action": action,
        "reuse_existing_reviewer": action == "DISPATCH_RE_REVIEW_TO_EXISTING_REVIEWER",
        "automatic_new_reviewer_creation": False,
        "new_task_creation_authority": "PARENT_ONLY",
        "operation_identity_input": operation_identity_input,
        "failures": failures,
    }


def validate_post_review_evidence(
    *,
    current_candidate_sha: str,
    reviewed_sha: str | None,
    reviewer_binding_proven: bool,
) -> dict[str, Any]:
    failures: list[str] = []
    if not reviewer_binding_proven:
        failures.append("REVIEWER_BINDING_UNPROVEN")
    if not current_candidate_sha or reviewed_sha != current_candidate_sha:
        failures.append("POST_REVIEW_SHA_MISMATCH")
    valid = not failures
    return {
        "schema_version": "2.1",
        "valid": valid,
        "current_candidate_sha": current_candidate_sha,
        "reviewed_sha": reviewed_sha,
        "exact_reviewed_sha_proven": valid,
        "failures": failures,
    }


def route_terminal_session_failure(*, same_session_available: bool) -> dict[str, Any]:
    if same_session_available:
        return {
            "schema_version": "2.1",
            "classification": "RECOVER_SAME_LINEAGE",
            "authority": "AUTO_SAFE",
            "action": "CONTINUE_SAME_SESSION",
            "automatic_new_task_creation": False,
        }
    return {
        "schema_version": "2.1",
        "classification": "SESSION_CONTINUATION_UNAVAILABLE",
        "authority": "PARENT_REQUIRED",
        "action": "NEW_TASK_RECOMMENDED",
        "automatic_new_task_creation": False,
        "new_task_creation_authority": "PARENT_ONLY",
    }
