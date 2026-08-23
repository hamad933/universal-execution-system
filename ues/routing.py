from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

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
    "POLICY_RESOLVABLE": "EXTERNAL_EFFECT_ELIGIBLE",
    "ENVIRONMENT_MISMATCH": "EXTERNAL_EFFECT_ELIGIBLE",
    "CI_DEPENDENT": "READ_ONLY",
    "REVIEW_DEPENDENT": "READ_ONLY",
    "TOOL_LIMIT": "EXTERNAL_EFFECT_ELIGIBLE",
    "SHARED_CONTRACT_REQUIRED": "PARENT_REQUIRED",
    "SCOPE_OR_NEW_TASK_REQUIRED": "PARENT_REQUIRED",
    "OWNER_DECISION_REQUIRED": "OWNER_REQUIRED",
    "UNCLASSIFIED": "DENY",
}

WAITING_SAME_SESSION_CONTINUATION = "WAITING_SAME_SESSION_CONTINUATION"
FAILURE_SAME_SESSION_RECOVERY = "FAILURE_SAME_SESSION_RECOVERY"
REVIEW_CORRECTION_PACKET = "REVIEW_CORRECTION_PACKET"
RE_REVIEW_DISPATCH = "RE_REVIEW_DISPATCH"

_EXTERNAL_WAITING_PREDICATES = {
    "POLICY_RESOLVABLE": "project_policy_permits",
    "ENVIRONMENT_MISMATCH": "bounded_workaround_authorized",
    "TOOL_LIMIT": "bounded_no_scope_expansion",
}

_PROVEN_WRITER_BINDING_KINDS = {"EXPLICIT", "DIRECT", "CANONICAL"}


def waiting_routing_table() -> dict[str, str]:
    return dict(WAITING_AUTHORITY)


def _normalize_actions(project_auto_safe_actions: Iterable[str] | None) -> set[str] | None:
    if project_auto_safe_actions is None:
        return None
    return {str(item).strip().upper() for item in project_auto_safe_actions if str(item).strip()}


def _external_effect_policy(
    effect: str,
    project_auto_safe_actions: Iterable[str] | None,
) -> tuple[str, list[str]]:
    actions = _normalize_actions(project_auto_safe_actions)
    if actions is None:
        return "DENY", ["PROJECT_AUTO_SAFE_ACTIONS_REQUIRED"]
    if effect not in actions:
        return "PARENT_REQUIRED", [f"PROJECT_ACTION_NOT_AUTO_SAFE_AUTHORIZED:{effect}"]
    return "AUTO_SAFE", [f"PROJECT_ACTION_AUTO_SAFE_AUTHORIZED:{effect}"]


def classify_waiting_activity(
    activity: Mapping[str, Any],
    *,
    provider_state: str,
    classifier_rules: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify waiting input only from explicit structured rules/evidence.

    Rule shape:
      {"rules": [{"waiting_class": "...", "match": {"field": value, ...},
                  "evidence": "stable-id"}]}

    ``provider_state`` may be used in ``match`` as the reserved field
    ``provider_state``. No free-text or keyword inference is performed.
    """

    normalized_state = str(provider_state or "UNKNOWN").strip().upper()
    rules = classifier_rules.get("rules") if isinstance(classifier_rules, Mapping) else None
    if not isinstance(rules, list):
        rules = []

    matches: list[tuple[str, dict[str, Any]]] = []
    for raw in rules:
        if not isinstance(raw, Mapping):
            continue
        waiting_class = str(raw.get("waiting_class") or "").strip().upper()
        expected = raw.get("match")
        if waiting_class not in WAITING_CLASSES or not isinstance(expected, Mapping):
            continue
        matched = True
        evidence: dict[str, Any] = {}
        for key, wanted in expected.items():
            observed = normalized_state if key == "provider_state" else activity.get(key)
            if observed != wanted:
                matched = False
                break
            evidence[str(key)] = observed
        if matched:
            if raw.get("evidence") is not None:
                evidence["rule_evidence"] = raw.get("evidence")
            matches.append((waiting_class, evidence))

    if len(matches) != 1:
        return {
            "schema_version": "2.2",
            "waiting_class": "UNCLASSIFIED",
            "classification_evidence": {
                "provider_state": normalized_state,
                "matched_rule_count": len(matches),
            },
            "confidence": "LOW",
            "keyword_shortcut_used": False,
            "authority": "POLICY_REQUIRED",
        }

    waiting_class, evidence = matches[0]
    return {
        "schema_version": "2.2",
        "waiting_class": waiting_class,
        "classification_evidence": {
            "provider_state": normalized_state,
            **evidence,
        },
        "confidence": "HIGH",
        "keyword_shortcut_used": False,
        "authority": "POLICY_REQUIRED",
    }


def route_waiting(
    waiting_class: str,
    *,
    exact_state_read: bool,
    latest_activity_read: bool,
    continuation_binding_proven: bool,
    project_auto_safe_actions: Iterable[str] | None,
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
    action = "STOP"
    reasons: list[str] = []
    effect = None

    if not exact_state_read or not latest_activity_read:
        authority = "DENY"
        reasons.append("EXACT_PROVIDER_STATE_AND_LATEST_ACTIVITY_REQUIRED")
    elif normalized == "CI_DEPENDENT":
        if deterministic_evidence:
            authority = "READ_ONLY"
            action = "RECONCILE_CI_EVIDENCE"
            reasons.append("DETERMINISTIC_CI_EVIDENCE_RECONCILIATION")
        else:
            authority = "DENY"
            reasons.append("DETERMINISTIC_EVIDENCE_REQUIRED")
    elif normalized == "REVIEW_DEPENDENT":
        if deterministic_evidence:
            authority = "READ_ONLY"
            action = "RECONCILE_REVIEW_EVIDENCE"
            reasons.append("DETERMINISTIC_REVIEW_EVIDENCE_RECONCILIATION")
        else:
            authority = "DENY"
            reasons.append("DETERMINISTIC_EVIDENCE_REQUIRED")
    elif normalized in _EXTERNAL_WAITING_PREDICATES:
        effect = WAITING_SAME_SESSION_CONTINUATION
        if not continuation_binding_proven:
            authority = "DENY"
            reasons.append("CONTINUATION_BINDING_UNPROVEN")
        elif not same_session_available:
            authority = "PARENT_REQUIRED"
            action = "PARENT_CONTINUATION_OR_NEW_TASK_RECOMMENDATION"
            reasons.append("SAME_SESSION_CONTINUATION_UNAVAILABLE")
        else:
            predicates = {
                "project_policy_permits": project_policy_permits,
                "bounded_workaround_authorized": bounded_workaround_authorized,
                "bounded_no_scope_expansion": bounded_no_scope_expansion,
            }
            predicate_name = _EXTERNAL_WAITING_PREDICATES[normalized]
            if not predicates[predicate_name]:
                authority = "DENY"
                reasons.append(f"AUTO_SAFE_PRECONDITION_MISSING:{predicate_name}")
            else:
                authority, policy_reasons = _external_effect_policy(
                    effect,
                    project_auto_safe_actions,
                )
                reasons.extend(policy_reasons)
                if authority == "AUTO_SAFE":
                    action = "CONTINUE_SAME_SESSION"
                elif authority == "PARENT_REQUIRED":
                    action = "ESCALATE_PARENT"
    elif generic_authority == "PARENT_REQUIRED":
        action = "ESCALATE_PARENT"
        reasons.append("PARENT_AUTHORITY_REQUIRED")
    elif generic_authority == "OWNER_REQUIRED":
        action = "ESCALATE_OWNER"
        reasons.append("OWNER_DECISION_REQUIRED")
    else:
        authority = "DENY"
        reasons.append("UNCLASSIFIED_FAIL_CLOSED")

    return {
        "schema_version": "2.2",
        "waiting_class": normalized,
        "generic_authority": generic_authority,
        "authority": authority,
        "action": action,
        "semantic_effect": effect,
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
    return [
        {
            "finding_id": finding.get("id"),
            "root_cause": group["root_cause"],
            "paths": list(finding.get("paths") or []),
        }
        for group in packet
        for finding in group["findings"]
    ]


def _operation_identity_input(
    *,
    project: str,
    route: str | None,
    workstream_id: str,
    effect_type: str,
    writer_session_id: str | None,
    reviewer_session_id: str | None,
    candidate_sha: str | None,
    reviewed_sha: str | None,
    effect_identity: Any,
) -> dict[str, Any]:
    return {
        "schema_version": "2.2",
        "contract": "CANONICAL_OPERATION_IDENTITY_INPUT",
        "identity_owner": "DOMAIN_D_OR_INTEGRATION",
        "action": effect_type,
        "project": project,
        "route": route,
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
    route: str | None = None,
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
    project_auto_safe_actions: Iterable[str] | None,
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
        route=route,
        workstream_id=workstream_id,
        effect_type=REVIEW_CORRECTION_PACKET,
        writer_session_id=writer_session_id,
        reviewer_session_id=reviewer_session_id,
        candidate_sha=candidate_sha,
        reviewed_sha=reviewed_sha,
        effect_identity=_finding_effect_identity(packet),
    )

    technical_routable = not failures
    authority = "DENY"
    action = "STOP"
    if technical_routable:
        authority, policy_failures = _external_effect_policy(
            REVIEW_CORRECTION_PACKET,
            project_auto_safe_actions,
        )
        failures.extend(policy_failures if authority != "AUTO_SAFE" else [])
        if authority == "AUTO_SAFE":
            action = "SEND_ONE_CORRECTION_PACKET_TO_EXISTING_WRITER"
        elif authority == "PARENT_REQUIRED":
            action = "ESCALATE_PARENT_REVIEW_CORRECTION_PACKET"

    return {
        "schema_version": "2.2",
        "project": project,
        "route": route,
        "workstream_id": workstream_id,
        "reviewed_sha": reviewed_sha,
        "candidate_sha": candidate_sha,
        "semantic_effect": REVIEW_CORRECTION_PACKET,
        "authority": authority,
        "action": action,
        "operation_identity_input": operation_identity_input,
        "correction_packet": packet if technical_routable else [],
        "grouped_packet_count": 1 if technical_routable else 0,
        "reuse_existing_writer": authority == "AUTO_SAFE",
        "automatic_new_task_creation": False,
        "failures": failures,
    }


def route_writer_to_reviewer(
    *,
    project: str,
    route: str | None = None,
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
    project_auto_safe_actions: Iterable[str] | None,
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
        authority, policy_failures = _external_effect_policy(
            RE_REVIEW_DISPATCH,
            project_auto_safe_actions,
        )
        failures.extend(policy_failures if authority != "AUTO_SAFE" else [])
        if authority == "AUTO_SAFE":
            action = "DISPATCH_RE_REVIEW_TO_EXISTING_REVIEWER"
        elif authority == "PARENT_REQUIRED":
            action = "ESCALATE_PARENT_RE_REVIEW_DISPATCH"
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
        route=route,
        workstream_id=workstream_id,
        effect_type=RE_REVIEW_DISPATCH,
        writer_session_id=writer_session_id,
        reviewer_session_id=reviewer_session_id,
        candidate_sha=new_candidate_sha,
        reviewed_sha=prior_reviewed_sha,
        effect_identity={
            "reason": "CANDIDATE_SHA_MOVED",
            "prior_review_stale": prior_review_stale,
        },
    )

    return {
        "schema_version": "2.2",
        "prior_review_stale": prior_review_stale,
        "new_candidate_sha": new_candidate_sha,
        "exact_required_ci_for_new_sha": exact_new_sha_ci,
        "pre_dispatch_review_evidence_required": False,
        "semantic_effect": RE_REVIEW_DISPATCH,
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
        "schema_version": "2.2",
        "valid": valid,
        "current_candidate_sha": current_candidate_sha,
        "reviewed_sha": reviewed_sha,
        "exact_reviewed_sha_proven": valid,
        "failures": failures,
    }


def route_terminal_session_failure(
    *,
    same_session_available: bool,
    project_auto_safe_actions: Iterable[str] | None,
) -> dict[str, Any]:
    if not same_session_available:
        return {
            "schema_version": "2.2",
            "classification": "SESSION_CONTINUATION_UNAVAILABLE",
            "semantic_effect": FAILURE_SAME_SESSION_RECOVERY,
            "authority": "PARENT_REQUIRED",
            "action": "NEW_TASK_RECOMMENDED",
            "automatic_new_task_creation": False,
            "new_task_creation_authority": "PARENT_ONLY",
            "failures": ["SAME_SESSION_CONTINUATION_UNAVAILABLE"],
        }

    authority, failures = _external_effect_policy(
        FAILURE_SAME_SESSION_RECOVERY,
        project_auto_safe_actions,
    )
    action = (
        "CONTINUE_SAME_SESSION"
        if authority == "AUTO_SAFE"
        else "ESCALATE_PARENT"
        if authority == "PARENT_REQUIRED"
        else "STOP"
    )
    return {
        "schema_version": "2.2",
        "classification": "RECOVER_SAME_LINEAGE",
        "semantic_effect": FAILURE_SAME_SESSION_RECOVERY,
        "authority": authority,
        "action": action,
        "automatic_new_task_creation": False,
        "failures": failures if authority != "AUTO_SAFE" else [],
    }
