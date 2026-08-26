from __future__ import annotations

import json
from typing import Any, Mapping

from .generation_pending import clear_pending_generation_transition, persist_pending_generation_transition
from .generation_transition import assess_generation_transition
from .lineage_effects import create_next_lineage_generation
from .lineage_generation import persist_created_generation_binding
from .lineage_registry import lineage_lane_id
from .state_store import StateUnavailable, record_unknown_write
from .task_budget_accounting import record_confirmed_generation


_REVIEW_ROLES = frozenset({"REVIEWER", "ASSURANCE", "FINAL_ASSURANCE"})
_CONTRACT_PREFIX = "PARENT_CONTROLLER_WORKSTREAM_CONTRACT_V1="


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
        "pending_generation_transition": evidence.get("pending_generation_transition"),
        "unknown_write_state": record.unknown_write_state,
        "action_in_flight": record.action_in_flight,
        "operation_state": (
            (record.operation_receipt or {}).get("state")
            if isinstance(record.operation_receipt, Mapping)
            else None
        ),
    }


def _replacement_review_contract(
    prompt: str,
    *,
    workstream: str,
    role: str,
    candidate_sha: str | None,
) -> dict[str, Any] | None:
    """Return a validated bounded contract embedded in a review replacement prompt.

    Same-lineage Reviewer/Assurance replacement generations are provider effects.
    They must not be created from a free-form recovery sentence alone. The Parent
    prompt therefore carries one compact JSON contract line prefixed by
    ``PARENT_CONTROLLER_WORKSTREAM_CONTRACT_V1=``. This validator is intentionally
    closed and fail-closed at the final provider-write boundary.
    """

    raw: str | None = None
    for line in str(prompt or "").splitlines():
        text = line.strip()
        if text.startswith(_CONTRACT_PREFIX):
            if raw is not None:
                return None
            raw = text[len(_CONTRACT_PREFIX) :].strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None

    contract = dict(value)
    required_text = (
        "objective",
        "exact_baseline",
        "role",
        "logical_lineage",
        "handoff",
        "stop_gate",
    )
    if any(not isinstance(contract.get(key), str) or not str(contract.get(key)).strip() for key in required_text):
        return None
    for key in ("write_scope", "prohibited_scope", "validation", "evidence"):
        items = contract.get(key)
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            return None
    if contract.get("write_scope") != []:
        return None
    if not contract.get("validation") or not contract.get("evidence"):
        return None

    expected_role = "ASSURANCE" if str(role).upper() == "FINAL_ASSURANCE" else str(role).upper()
    contract_role = "ASSURANCE" if str(contract.get("role") or "").upper() == "FINAL_ASSURANCE" else str(contract.get("role") or "").upper()
    if contract_role != expected_role:
        return None
    if str(contract.get("logical_lineage") or "").strip() != str(workstream).strip():
        return None

    baseline = str(contract.get("exact_baseline") or "").strip()
    _, sep, sha = baseline.rpartition("@")
    if not sep or not candidate_sha or sha.lower() != str(candidate_sha).lower():
        return None
    return contract


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
    """Create one lawful physical generation and durably bind/account it.

    The exact transition is persisted before the provider write. Any ambiguous
    provider result therefore has a durable reconciliation key and title marker;
    a subsequent runner must reconcile that UNKNOWN state instead of issuing a
    blind retry.
    """

    role_name = str(role).upper()
    if role_name in _REVIEW_ROLES and _replacement_review_contract(
        prompt,
        workstream=workstream,
        role=role_name,
        candidate_sha=candidate_sha,
    ) is None:
        return {
            "decision": "NEXT_GENERATION_WORKSTREAM_CONTRACT_REQUIRED",
            "provider_write_attempted": False,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "safe_to_blind_retry": False,
        }

    state_role = "ASSURANCE" if role_name == "FINAL_ASSURANCE" else role
    lane_id = lineage_lane_id(project, route, workstream, state_role)
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

    pending = persist_pending_generation_transition(
        store,
        project=project,
        route=route,
        workstream=workstream,
        role=role,
        transition=assessment,
        source_repository=repository,
        source_name=source_name,
        starting_branch=starting_branch,
        candidate_sha=candidate_sha,
        replacement_cause=replacement_cause,
    )

    next_generation = int(assessment["next_generation"])
    effect = create_next_lineage_generation(
        store,
        client,
        project=project,
        route=route,
        workstream=workstream,
        role=state_role,
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
        return {**effect, "transition": assessment, "pending_transition": pending}

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
                    "generation_transition_key": transition_key,
                    "safe_to_blind_retry": False,
                },
            )
        return {
            **effect,
            "decision": "GENERATION_BINDING_RECONCILIATION_REQUIRED",
            "transition": assessment,
            "pending_transition": pending,
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
        accounting = record_confirmed_generation(
            store,
            project=project,
            route=route,
            operation_key=operation_key,
            generation_transition_key=transition_key,
        )
        clear_pending_generation_transition(
            store,
            project=project,
            route=route,
            workstream=workstream,
            role=role,
            expected_transition_key=transition_key,
        )
    except Exception as exc:
        try:
            record_unknown_write(
                store,
                lane_id=lane_id,
                operation_key=operation_key,
                result={
                    "category": "STATESTORE_GENERATION_HANDOFF_FAILED",
                    "generation_transition_key": transition_key,
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
            "pending_transition": pending,
            "safe_to_blind_retry": False,
        }

    after = _state_snapshot(store, lane_id)
    if (
        int(after.get("generation") or 0) != next_generation
        or str(after.get("session_fingerprint") or "").lower() != new_fp
        or str(after.get("generation_transition_key") or "") != transition_key
        or after.get("pending_generation_transition") is not None
    ):
        raise StateUnavailable("created generation StateStore post-readback mismatch")

    return {
        **effect,
        "decision": "BINDING_SAFE_NEXT_GENERATION_CONFIRMED",
        "transition": assessment,
        "generation_binding": binding,
        "budget_accounting": accounting,
        "generation": next_generation,
        "session_fingerprint": new_fp,
        "safe_to_blind_retry": False,
    }
