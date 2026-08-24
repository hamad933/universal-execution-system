from __future__ import annotations

from typing import Any, Mapping

SCHEMA_VERSION = "1.0"


def plan_recovery(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Map an observed lifecycle condition to an executable next transition.

    A classification is never treated as a Stop Gate by itself. The result
    distinguishes controller/runtime-remediable actions from genuine external
    gates and always names the exact remaining cause.
    """

    binding = str(observation.get("binding_status") or "UNBOUND").upper()
    state = str(observation.get("provider_state") or "UNKNOWN").upper()
    role = str(observation.get("role") or "").upper()
    handoff = observation.get("handoff") if isinstance(observation.get("handoff"), Mapping) else {}
    handoff_status = str(handoff.get("status") or "").upper()
    verdict = str(handoff.get("verdict") or "UNKNOWN").upper()
    candidate_sha = str(observation.get("candidate_sha") or handoff.get("candidate_sha") or "")
    reviewed_sha = str(handoff.get("reviewed_sha") or "")
    current_sha = str(observation.get("current_sha") or "")
    ci_reason = str(observation.get("ci_reason") or "")
    waiting_has_newer_user = observation.get("waiting_has_newer_or_equal_user_response")
    work_remaining = bool(observation.get("work_remaining", True))
    budget_safe = bool(observation.get("new_session_budget_safe", False))
    replacement_prompt_ready = bool(observation.get("replacement_prompt_ready", False))

    if binding == "AMBIGUOUS":
        return _decision(
            "RECONCILE_EXACT_LINEAGE_BINDING",
            root_cause="MULTIPLE_EXACT_LINEAGE_SESSION_MATCHES",
            executable=True,
        )
    if binding == "UNBOUND":
        if work_remaining and budget_safe and replacement_prompt_ready:
            return _decision(
                "CREATE_OR_ADOPT_SAME_LOGICAL_LINEAGE_GENERATION",
                root_cause="SESSION_BINDING_MISSING",
                executable=True,
                external_effect=True,
            )
        return _decision(
            "RECONCILE_BINDING_OR_PREPARE_SAME_LINEAGE_REPLACEMENT",
            root_cause="SESSION_BINDING_MISSING",
            executable=not work_remaining or replacement_prompt_ready,
            stop_gate="NEW_SESSION_AUTHORITY_OR_BUDGET_REQUIRED" if work_remaining else None,
        )

    if current_sha and reviewed_sha and current_sha.lower() != reviewed_sha.lower():
        return _decision(
            "INVALIDATE_STALE_REVIEW_AND_ROUTE_CURRENT_SHA",
            root_cause="REVIEW_SHA_STALE",
            executable=True,
        )

    if state == "AWAITING_USER_FEEDBACK":
        if waiting_has_newer_user is True:
            return _decision(
                "WAIT_FOR_AGENT_ON_SAME_SESSION",
                root_cause="WAITING_ALREADY_HAS_NEWER_OR_EQUAL_USER_RESPONSE",
                executable=True,
            )
        if observation.get("same_session_prompt_ready"):
            return _decision(
                "CONTINUE_SAME_SESSION",
                root_cause="WAITING_INPUT_CONTROLLER_RESOLVABLE",
                executable=True,
                external_effect=True,
            )
        return _decision(
            "RESOLVE_WAITING_INPUT_CONTENT_OR_POLICY",
            root_cause="WAITING_INPUT_REQUIRES_RECONCILIATION",
            executable=True,
        )

    if state == "IN_PROGRESS":
        return _decision(
            "OBSERVE_SAME_SESSION_PROGRESS",
            root_cause="SESSION_ACTIVE",
            executable=True,
        )

    if state in {"QUEUED", "PLANNING"}:
        return _decision(
            "WAIT_ACTIVE_SAME_SESSION",
            root_cause=f"SESSION_{state}",
            executable=True,
        )

    if state == "AWAITING_PLAN_APPROVAL":
        return _decision(
            "APPROVE_PLAN_OR_PARENT_RECONCILE_SAME_SESSION",
            root_cause="PLAN_APPROVAL_REQUIRED",
            executable=True,
        )

    if state == "PAUSED":
        return _decision(
            "RECONCILE_PAUSED_SAME_SESSION",
            root_cause="SESSION_PAUSED",
            executable=True,
        )

    if state == "COMPLETED":
        if role == "WRITER":
            if handoff_status == "CONTEXT_EXHAUSTED" and work_remaining:
                return _replacement_decision(budget_safe, replacement_prompt_ready, "CONTEXT_EXHAUSTED_REPORTED")
            if candidate_sha:
                if ci_reason == "REQUIRED_CI_MISSING":
                    return _decision(
                        "DIAGNOSE_AND_REMEDIATE_CI_TRIGGER",
                        root_cause="CI_TRIGGER_OR_EVIDENCE_MISSING",
                        executable=True,
                    )
                if str(observation.get("ci_verdict") or "").upper() == "PASS":
                    return _decision(
                        "ROUTE_CURRENT_SHA_TO_REVIEWER_LINEAGE",
                        root_cause="WRITER_COMPLETED_CANDIDATE_READY_FOR_REVIEW",
                        executable=True,
                    )
                return _decision(
                    "VALIDATE_EXACT_WRITER_CANDIDATE",
                    root_cause="WRITER_COMPLETED_REQUIRES_EXACT_HEAD_EVIDENCE",
                    executable=True,
                )
            if work_remaining:
                return _replacement_decision(budget_safe, replacement_prompt_ready, "WRITER_COMPLETED_MORE_WORK_REQUIRED")
            return _decision("CLOSE_WRITER_LINEAGE", root_cause="WRITER_WORK_COMPLETE", executable=True)

        if role in {"REVIEWER", "ASSURANCE"}:
            if verdict == "PASS" and reviewed_sha and (not current_sha or reviewed_sha.lower() == current_sha.lower()):
                return _decision(
                    "MARK_CURRENT_SHA_REVIEW_PASS",
                    root_cause="STRUCTURED_REVIEW_PASS_CURRENT_SHA",
                    executable=True,
                )
            if verdict in {"FINDINGS", "FAIL"}:
                return _decision(
                    "ROUTE_STRUCTURED_FINDINGS_TO_WRITER_LINEAGE",
                    root_cause="REVIEW_FINDINGS_REQUIRE_CORRECTION",
                    executable=True,
                    external_effect=True,
                )
            if work_remaining:
                return _replacement_decision(budget_safe, replacement_prompt_ready, "REVIEWER_COMPLETED_MORE_REVIEW_REQUIRED")
            return _decision(
                "PARENT_ADJUDICATE_UNSTRUCTURED_COMPLETED_REVIEW",
                root_cause="COMPLETED_REVIEW_OUTPUT_UNSTRUCTURED_OR_UNBOUND",
                executable=True,
            )

    if state == "FAILED":
        if not work_remaining:
            return _decision(
                "CLOSE_FAILED_LINEAGE_NO_REPLACEMENT",
                root_cause="TERMINAL_FAILURE_NO_MORE_WORK_REQUIRED",
                executable=True,
            )
        return _replacement_decision(budget_safe, replacement_prompt_ready, "TERMINAL_FAILED_SESSION")

    if observation.get("unknown_write_state"):
        return _decision(
            "AUTHORITATIVE_POST_WRITE_RECONCILIATION",
            root_cause="UNKNOWN_PROVIDER_WRITE",
            executable=True,
        )

    return _decision(
        "ROOT_CAUSE_RECONCILIATION_REQUIRED",
        root_cause="UNCLASSIFIED_LIFECYCLE_STATE",
        executable=True,
    )


def _replacement_decision(budget_safe: bool, prompt_ready: bool, reason: str) -> dict[str, Any]:
    if budget_safe and prompt_ready:
        return _decision(
            "CREATE_NEXT_SESSION_GENERATION_SAME_LINEAGE",
            root_cause=reason,
            executable=True,
            external_effect=True,
        )
    missing = []
    if not budget_safe:
        missing.append("TASK_BUDGET_OR_NEW_SESSION_AUTHORITY")
    if not prompt_ready:
        missing.append("EXACT_REPLACEMENT_TASK_SPEC")
    return _decision(
        "PREPARE_SAME_LINEAGE_REPLACEMENT",
        root_cause=reason,
        executable=True,
        stop_gate="+".join(missing) if missing else None,
    )


def _decision(
    action: str,
    *,
    root_cause: str,
    executable: bool,
    external_effect: bool = False,
    stop_gate: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "root_cause": root_cause,
        "controller_or_runtime_executable": bool(executable),
        "external_effect": bool(external_effect),
        "stop_gate": stop_gate,
        "generic_blocker_is_stop_gate": False,
    }
