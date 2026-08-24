from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .project_adapter import ProjectAdapter, build_required_evidence_profile
from .routing import classify_waiting_activity, route_waiting
from .task_budget import evaluate_task_budget, evaluate_new_task_gate


def _shadow_budget(
    adapter: ProjectAdapter,
    observation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    observed = observation or {}
    policy = adapter.raw.get("task_budget")
    policy = policy if isinstance(policy, Mapping) else {}
    ceiling = policy.get("ceiling")
    reserve = policy.get("reserve_target")
    reserve_status = str(policy.get("reserve_status") or "").upper()
    lifetime_known = bool(observed.get("lifetime_consumption_known"))
    proven_used = observed.get("proven_lifetime_used")
    current_count = observed.get("current_enumerated_tasks")

    if not isinstance(ceiling, int):
        return {
            "schema_version": "2.1",
            "project": adapter.project,
            "state": "TASK_BUDGET_POLICY_INCOMPLETE",
            "ceiling": ceiling,
            "reserve": reserve,
            "proven_lifetime_used": proven_used,
            "current_enumerated_tasks": current_count,
            "safe_remaining": None,
            "budget_allows_new_task": False,
            "new_task_creation_authority": "PARENT_ONLY",
            "automatic_new_task_creation": False,
            "fail_closed": True,
            "current_enumeration_proves_lifetime_consumption": False,
        }

    if isinstance(reserve, int):
        effective_reserve = reserve
    elif reserve is None and reserve_status == "NOT_DEFINED_BY_CURRENT_GS_AUTHORITY":
        # Arithmetic representation of the governed absence of a reserve target;
        # this does not invent a new reserve policy.
        effective_reserve = 0
    else:
        return {
            "schema_version": "2.1",
            "project": adapter.project,
            "state": "TASK_BUDGET_POLICY_INCOMPLETE",
            "ceiling": ceiling,
            "reserve": reserve,
            "proven_lifetime_used": proven_used,
            "current_enumerated_tasks": current_count,
            "safe_remaining": None,
            "budget_allows_new_task": False,
            "new_task_creation_authority": "PARENT_ONLY",
            "automatic_new_task_creation": False,
            "fail_closed": True,
            "current_enumeration_proves_lifetime_consumption": False,
        }

    if lifetime_known and proven_used is None:
        return {
            "schema_version": "2.1",
            "project": adapter.project,
            "state": "TASK_BUDGET_EVIDENCE_INCOMPLETE",
            "ceiling": ceiling,
            "reserve": effective_reserve,
            "proven_lifetime_used": None,
            "current_enumerated_tasks": current_count,
            "safe_remaining": None,
            "budget_allows_new_task": False,
            "new_task_creation_authority": "PARENT_ONLY",
            "automatic_new_task_creation": False,
            "fail_closed": True,
            "current_enumeration_proves_lifetime_consumption": False,
        }

    return evaluate_task_budget(
        project=adapter.project,
        ceiling=ceiling,
        reserve=effective_reserve,
        lifetime_consumption_known=lifetime_known,
        proven_lifetime_used=(int(proven_used) if proven_used is not None else None),
        current_enumerated_tasks=(int(current_count) if current_count is not None else None),
        unknown_lifetime_policy=adapter.unknown_lifetime_capacity,
    )


def evaluate_project_shadow(
    adapter: ProjectAdapter,
    *,
    evidence_observations: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    task_budget_observation: Mapping[str, Any] | None = None,
    waiting_observations: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Evaluate project policy/evidence in SHADOW without dispatching effects.

    All mutable facts are caller-supplied observations. The adapter contributes
    stable project policy only. This function never reads secrets, writes runtime
    state, sends provider messages, creates tasks, or mutates GitHub.
    """

    observed_profiles = evidence_observations or {}
    evidence: dict[str, Any] = {}
    for profile_name in sorted(adapter.evidence_profiles):
        profile = build_required_evidence_profile(
            adapter,
            profile_name,
            observed_profiles.get(profile_name, {}),
        )
        issues = profile.issues_for(None)
        evidence[profile_name] = {
            "profile_id": profile.profile_id,
            "complete": not issues,
            "issues": list(issues),
        }

    budget = _shadow_budget(adapter, task_budget_observation)
    new_task_gate = evaluate_new_task_gate(budget, parent_gate_satisfied=False)

    waiting_results: list[dict[str, Any]] = []
    external_effect_candidates: list[dict[str, Any]] = []
    for observation in waiting_observations:
        activity = observation.get("activity")
        activity = activity if isinstance(activity, Mapping) else {}
        provider_state = str(observation.get("provider_state") or "UNKNOWN")
        classification = classify_waiting_activity(
            activity,
            provider_state=provider_state,
            classifier_rules=adapter.waiting_classifier_rules,
        )
        route = route_waiting(
            classification["waiting_class"],
            exact_state_read=bool(observation.get("exact_state_read")),
            latest_activity_read=bool(observation.get("latest_activity_read")),
            continuation_binding_proven=bool(observation.get("continuation_binding_proven")),
            project_auto_safe_actions=adapter.project_auto_safe_actions,
            same_session_available=bool(observation.get("same_session_available", True)),
            project_policy_permits=bool(observation.get("project_policy_permits")),
            bounded_workaround_authorized=bool(observation.get("bounded_workaround_authorized")),
            deterministic_evidence=bool(observation.get("deterministic_evidence")),
            bounded_no_scope_expansion=bool(observation.get("bounded_no_scope_expansion")),
        )
        if route.get("semantic_effect"):
            external_effect_candidates.append(
                {
                    "semantic_effect": route["semantic_effect"],
                    "authority": route["authority"],
                    "action": route["action"],
                }
            )
        waiting_results.append(
            {
                "classification": classification,
                "route": route,
            }
        )

    return {
        "schema_version": "2.1",
        "project": adapter.project,
        "route": adapter.route,
        "repository": adapter.repository,
        "activation_mode": "SHADOW",
        "mutation_allowed": False,
        "config_grants_mutation_authority": False,
        "external_effects_dispatched": 0,
        "tasks_or_sessions_created": 0,
        "evidence": evidence,
        "task_budget": budget,
        "new_task_gate": new_task_gate,
        "waiting": waiting_results,
        "external_effect_candidates": external_effect_candidates,
    }
