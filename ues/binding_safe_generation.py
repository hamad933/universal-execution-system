from __future__ import annotations

from typing import Any, Mapping

from .generation_transition import assess_generation_transition
from .lineage_effects import create_next_lineage_generation
from .lineage_generation import persist_created_generation_binding
from .lineage_registry import lineage_lane_id
from .state_store import StateUnavailable, record_unknown_write


def _state_snapshot(store: Any, lane_id: str) -> dict[str, Any]:
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        return {}
    record = read.record
    evidence = record.evidence_bindings or {}
    return {
        "generation": int(evidence.get("generation") or 0),
        "session_fingerprint": evidence.get("session_fingerprint"),
        "candidate_sha": evidence.get("current_candidate_sha"),
        "generation_transition_key": evidence.get("generation_transition_key"),
        "unknown_write_state": record.unknown_write_state,
        "action_in_flight": record.action_in_flight,
        "operation_state": (
            (record.operation_receipt or {}).get("state")
            if isinstance(record.operation_receipt, Mapping)
            else None
        ),
    }


def execute_binding_safe_generation(
    store: Any,
    client: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    prompt: str,
    title: str,
    source_name: str,
    starting_branch: str,
    repository: str,
    authority_event_id: str,
    current_policy: Mapping[str, Any],
    replacement_cause: str,
    candidate_sha: str | None,
    work_remaining: bool,
    active_duplicate_absent: bool,
    exact_repository_binding: bool,
    exact_starting_ref_binding: bool,
) -> dict[str, Any]:
    """Create exactly one lawful physical generation and durably bind it.

    The lower-level provider effect remains reusable, but this is the runtime
    entry point for automatic next-generation creation. It requires current
    project policy, replacement proof, duplicate/UNKNOWN reconciliation and
    StateStore handoff before the transition is considered complete.
    """

    lane_id = lineage_lane_id(
        project,
        route,
        workstream,
        "ASSURANCE" if str(role).upper() == "FINAL_ASSURANCE" else role,
    )
    before = _state_snapshot(store, lane_id)
    current_generation = int(before.get("generation") or 0)
    predecessor = str(before.get("session_fingerprint") or "").strip() or None

    assessment = assess_generation_transition(
        project=project,
        route=route,
        workstream=workstream,
        role=role,
        current_generation=current_generation,
        predecessor_session_fingerprint=predecessor,
        candidate_sha=candidate_sha,
        replacement_cause=replacement_cause,
        work_remaining=work_remaining,
        current_policy=current_policy,
        active_duplicate_absent=active_duplicate_absent,
        unknown_write_state=bool(before.get("unknown_write_state")),
        exact_repository_binding=exact_repository_binding,
        exact_starting_ref_binding=exact_starting_ref_binding,
        replacement_task_spec_ready=bool(prompt.strip() and title.strip()),
    )
    if not assessment["allowed"]:
        return {
            "decision": "NEXT_GENERATION_BLOCKED",
            "provider_write_attempted": False,
            "transition": assessment,
            "safe_to_blind_retry": False,
        }

    transition_key = str(assessment["transition_key"])
    if before.get("generation_transition_key") == transition_key and before.get("session_fingerprint"):
        return {
            "decision": "IDEMPOTENT_GENERATION_TRANSITION_CONFIRMED",
            "provider_write_attempted": False,
            "transition": assessment,
            "generation": current_generation,
            "session_fingerprint": before.get("session_fingerprint"),
            "safe_to_blind_retry": False,
        }

    next_generation = int(assessment["next_generation"])
    effect = create_next_lineage_generation(
        store,
        client,
        project=project,
        route=route,
        workstream=workstream,
        role="ASSURANCE" if str(role).upper() == "FINAL_ASSURANCE" else role,
        predecessor_session_fingerprint=predecessor,
        next_generation=next_generation,
        prompt=prompt,
        title=f"{title} [{transition_key[:12]}]",
        source_name=source_name,
        starting_branch=starting_branch,
        repository=repository,
        authority_event_id=authority_event_id,
        budget_safe=True,
    )

    decision = str(effect.get("decision") or "")
    if decision not in {"NEXT_SESSION_GENERATION_CONFIRMED", "IDEMPOTENT_REPLAY_CONFIRMED"}:
        return {**effect, "transition": assessment}

    new_fp = str(effect.get("session_fingerprint") or "").strip().lower()
    operation_key = str(effect.get("operation_key") or "").strip()
    if not new_fp or not operation_key:
        if operation_key:
            record_unknown_write(
                store,
                lane_id=lane_id,
                operation_key=operation_key,
                result={
                    "category": "GENERATION_CONFIRMATION_MISSING_BINDING",
                    "safe_to_blind_retry": False,
                },
            )
        return {
            **effect,
            "decision": "GENERATION_BINDING_RECONCILIATION_REQUIRED",
            "transition": assessment,
            "safe_to_blind_retry": False,
        }

    try:
        binding = persist_created_generation_binding(
            store,
            project=project,
            route=route,
            workstream=workstream,
            role=role,
            generation=next_generation,
            session_fingerprint=new_fp,
            source_name=source_name,
            source_repository=repository,
            provider_starting_branch=starting_branch,
            authority_event_id=authority_event_id,
            operation_key=operation_key,
            generation_transition_key=transition_key,
            replacement_cause=replacement_cause,
            candidate_sha=candidate_sha,
            policy_provenance=current_policy.get("provenance") if isinstance(current_policy.get("provenance"), Mapping) else {},
        )
    except Exception as exc:
        try:
            record_unknown_write(
                store,
                lane_id=lane_id,
                operation_key=operation_key,
                result={
                    "category": "STATESTORE_GENERATION_BINDING_PERSISTENCE_FAILED",
                    "error_type": type(exc).__name__,
                    "safe_to_blind_retry": False,
                },
            )
        except Exception:
            pass
        return {
            **effect,
            "decision": "GENERATION_CREATED_STATESTORE_RECONCILIATION_REQUIRED",
            "transition": assessment,
            "safe_to_blind_retry": False,
        }

    after = _state_snapshot(store, lane_id)
    if (
        int(after.get("generation") or 0) != next_generation
        or str(after.get("session_fingerprint") or "").lower() != new_fp
        or str(after.get("generation_transition_key") or "") != transition_key
    ):
        raise StateUnavailable("created generation StateStore post-readback mismatch")

    return {
        **effect,
        "decision": "BINDING_SAFE_NEXT_GENERATION_CONFIRMED",
        "transition": assessment,
        "generation_binding": binding,
        "generation": next_generation,
        "session_fingerprint": new_fp,
        "safe_to_blind_retry": False,
    }
