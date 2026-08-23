from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from ..control_loop import run_shadow_cycle
from ..lifecycle import FailureClass, LifecycleState, NextAction, WaitingClass
from ..reconciliation import (
    ActorBinding,
    CIBinding,
    EvidenceRequirement,
    RequiredEvidenceProfile,
    ReviewBinding,
    WorkstreamBinding,
)
from ..routing import (
    classify_waiting_activity,
    route_reviewer_to_writer,
    route_terminal_session_failure,
    route_waiting,
    route_writer_to_reviewer,
)
from ..state_store import StateStore
from ..task_budget import evaluate_task_budget

PROJECT_ID = "CEP"
ROUTE = "PERSONAL:CEP"
REPOSITORY = "hamad933/Cybersecurity-Education-Platform"
TASK_CEILING = 70
TASK_RESERVE = 15

# Current governed CEP authority is SHADOW-required and does not grant broad
# autonomous external effects. A future governed action policy must be supplied
# explicitly; missing policy stays fail-closed.
DEFAULT_AUTO_SAFE_ACTIONS: frozenset[str] = frozenset()

# These are normalized structured evidence fields owned by the adapter boundary,
# not keyword matches against provider prose. They describe the bounded class of
# an existing same-session question whose answer is Controller-resolvable without
# scope expansion. Classification still grants no mutation authority by itself.
DEFAULT_WAITING_CLASSIFIER_RULES: Mapping[str, Any] = {
    "rules": [
        {
            "waiting_class": "POLICY_RESOLVABLE",
            "match": {
                "provider_state": "AWAITING_USER_FEEDBACK",
                "question_scope": "CONTROLLER_RESOLVABLE",
                "continuation_scope": "SAME_SESSION",
                "scope_expansion": False,
            },
            "evidence": "cep-structured-same-session-controller-input-v1",
        }
    ]
}


def auto_safe_actions(authoritative_actions: Iterable[str] | None = None) -> frozenset[str]:
    if authoritative_actions is None:
        return DEFAULT_AUTO_SAFE_ACTIONS
    return frozenset(
        str(item).strip().upper()
        for item in authoritative_actions
        if str(item).strip()
    )


def classify_waiting_shadow(
    activity: Mapping[str, Any],
    *,
    provider_state: str,
    classifier_rules: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return classify_waiting_activity(
        activity,
        provider_state=provider_state,
        classifier_rules=classifier_rules or DEFAULT_WAITING_CLASSIFIER_RULES,
    )


def build_evidence_profile(
    *,
    core_ci_proven: bool,
    release_browser_verification_proven: bool,
    exact_sha_review_proven: bool,
    route_specific_browser_proven: bool | None = None,
    architecture_contract_proven: bool | None = None,
    core_ci_evidence_id: str | None = None,
    release_browser_evidence_id: str | None = None,
    exact_sha_review_evidence_id: str | None = None,
    route_browser_evidence_id: str | None = None,
    architecture_evidence_id: str | None = None,
) -> RequiredEvidenceProfile:
    parent_actions = (NextAction.REQUEST_PARENT_REVIEW.value,)
    review_actions = (
        NextAction.START_EXACT_SHA_REVIEW.value,
        NextAction.START_RE_REVIEW.value,
        NextAction.REQUEST_PARENT_REVIEW.value,
    )
    requirements = [
        EvidenceRequirement(
            "cep_core_ci",
            core_ci_proven,
            evidence_id=core_ci_evidence_id,
            actions=review_actions,
        ),
        EvidenceRequirement(
            "cep_release_and_browser_verification",
            release_browser_verification_proven,
            evidence_id=release_browser_evidence_id,
            actions=parent_actions,
        ),
        EvidenceRequirement(
            "cep_exact_sha_review",
            exact_sha_review_proven,
            evidence_id=exact_sha_review_evidence_id,
            actions=parent_actions,
        ),
    ]
    if route_specific_browser_proven is not None:
        requirements.append(
            EvidenceRequirement(
                "cep_route_specific_browser_evidence",
                route_specific_browser_proven,
                evidence_id=route_browser_evidence_id,
                actions=parent_actions,
            )
        )
    if architecture_contract_proven is not None:
        requirements.append(
            EvidenceRequirement(
                "cep_architecture_contract",
                architecture_contract_proven,
                evidence_id=architecture_evidence_id,
                actions=parent_actions,
            )
        )
    return RequiredEvidenceProfile("cep-shadow-evidence-v1", tuple(requirements))


def build_binding(
    *,
    workstream: str,
    role: str,
    branch: str,
    base_ref: str,
    baseline_sha: str,
    lifecycle_state: LifecycleState,
    last_activity_at: datetime,
    task_budget_class: str = "PARENT_ONLY",
    pr_number: int | None = None,
    head_sha: str | None = None,
    actor_bindings: Iterable[ActorBinding] = (),
    ci: CIBinding | None = None,
    review: ReviewBinding | None = None,
    waiting_class: WaitingClass | None = None,
    error_class: FailureClass | None = None,
    resume_state: LifecycleState | None = None,
    scope_identity: str | None = None,
    evidence_profile: RequiredEvidenceProfile | None = None,
    stop_gate: str | None = None,
) -> WorkstreamBinding:
    return WorkstreamBinding(
        project=PROJECT_ID,
        route=ROUTE,
        workstream=workstream,
        role=role,
        repo=REPOSITORY,
        branch=branch,
        lifecycle_state=lifecycle_state,
        baseline_sha=baseline_sha,
        base_ref=base_ref,
        task_budget_class=task_budget_class,
        last_activity_at=last_activity_at,
        actor_bindings=tuple(actor_bindings),
        scope_identity=scope_identity,
        evidence_profile=evidence_profile,
        pr_number=pr_number,
        head_sha=head_sha,
        ci=ci,
        review=review,
        waiting_class=waiting_class,
        error_class=error_class,
        resume_state=resume_state,
        stop_gate=stop_gate,
    )


def validate_binding(binding: WorkstreamBinding) -> None:
    expected = (PROJECT_ID, ROUTE, REPOSITORY)
    observed = (binding.project, binding.route, binding.repo)
    if observed != expected:
        raise ValueError(
            "CEP adapter identity mismatch: expected project/route/repository "
            f"{expected!r}, got {observed!r}"
        )


def task_budget_snapshot(
    *,
    current_enumerated_tasks: int | None,
    lifetime_consumption_known: bool = False,
    proven_lifetime_used: int | None = None,
) -> dict[str, Any]:
    return evaluate_task_budget(
        project=PROJECT_ID,
        ceiling=TASK_CEILING,
        reserve=TASK_RESERVE,
        lifetime_consumption_known=lifetime_consumption_known,
        proven_lifetime_used=proven_lifetime_used,
        current_enumerated_tasks=current_enumerated_tasks,
    )


def route_waiting_shadow(
    waiting_class: str,
    *,
    exact_state_read: bool,
    latest_activity_read: bool,
    continuation_binding_proven: bool,
    authoritative_actions: Iterable[str] | None = None,
    same_session_available: bool = True,
    project_policy_permits: bool = False,
    bounded_workaround_authorized: bool = False,
    deterministic_evidence: bool = False,
    bounded_no_scope_expansion: bool = False,
) -> dict[str, Any]:
    return route_waiting(
        waiting_class,
        exact_state_read=exact_state_read,
        latest_activity_read=latest_activity_read,
        continuation_binding_proven=continuation_binding_proven,
        project_auto_safe_actions=auto_safe_actions(authoritative_actions),
        same_session_available=same_session_available,
        project_policy_permits=project_policy_permits,
        bounded_workaround_authorized=bounded_workaround_authorized,
        deterministic_evidence=deterministic_evidence,
        bounded_no_scope_expansion=bounded_no_scope_expansion,
    )


def route_reviewer_findings_shadow(
    *,
    workstream: str,
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
    authoritative_actions: Iterable[str] | None = None,
) -> dict[str, Any]:
    return route_reviewer_to_writer(
        project=PROJECT_ID,
        route=ROUTE,
        workstream_id=workstream,
        writer_session_id=writer_session_id,
        reviewer_session_id=reviewer_session_id,
        reviewed_sha=reviewed_sha,
        candidate_sha=candidate_sha,
        reviewer_role_valid=reviewer_role_valid,
        reviewer_independent=reviewer_independent,
        reviewer_mutation_detected=reviewer_mutation_detected,
        reviewer_mutation_adjudicated=reviewer_mutation_adjudicated,
        reviewer_mutation_disqualifying=reviewer_mutation_disqualifying,
        writer_binding_proven=writer_binding_proven,
        writer_binding_kind=writer_binding_kind,
        finding_within_writer_scope=finding_within_writer_scope,
        canonical_operation_active=canonical_operation_active,
        canonical_operation_confirmed=canonical_operation_confirmed,
        findings=findings,
        project_auto_safe_actions=auto_safe_actions(authoritative_actions),
    )


def route_rereview_shadow(
    *,
    workstream: str,
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
    authoritative_actions: Iterable[str] | None = None,
) -> dict[str, Any]:
    return route_writer_to_reviewer(
        project=PROJECT_ID,
        route=ROUTE,
        workstream_id=workstream,
        writer_session_id=writer_session_id,
        reviewer_session_id=reviewer_session_id,
        prior_reviewed_sha=prior_reviewed_sha,
        new_candidate_sha=new_candidate_sha,
        ci_evidence_sha=ci_evidence_sha,
        required_ci_proven=required_ci_proven,
        existing_reviewer_available=existing_reviewer_available,
        existing_reviewer_binding_proven=existing_reviewer_binding_proven,
        existing_reviewer_safe_to_reuse=existing_reviewer_safe_to_reuse,
        new_reviewer_policy_allows=new_reviewer_policy_allows,
        parent_gate_satisfied=parent_gate_satisfied,
        project_auto_safe_actions=auto_safe_actions(authoritative_actions),
    )


def route_terminal_failure_shadow(
    *,
    same_session_available: bool,
    authoritative_actions: Iterable[str] | None = None,
) -> dict[str, Any]:
    return route_terminal_session_failure(
        same_session_available=same_session_available,
        project_auto_safe_actions=auto_safe_actions(authoritative_actions),
    )


def run_cep_shadow_cycle(
    bindings: Iterable[WorkstreamBinding],
    *,
    previous_by_lane: Mapping[tuple[str, str, str], WorkstreamBinding] | None = None,
    state_store: StateStore | None = None,
    watchdog_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = tuple(bindings)
    for binding in items:
        validate_binding(binding)
    result = run_shadow_cycle(
        items,
        previous_by_lane=previous_by_lane,
        state_store=state_store,
        watchdog_policy=watchdog_policy,
    )
    return {
        "schema_version": "1.0",
        "adapter": "CEP_SHADOW",
        "project": PROJECT_ID,
        "route": ROUTE,
        "repository": REPOSITORY,
        "authority_default": "NO_AUTONOMOUS_EXTERNAL_EFFECT",
        "automatic_new_task_creation": False,
        "cycle": result,
    }
