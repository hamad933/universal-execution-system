from __future__ import annotations

from typing import Any

from .identity import canonical_lane_id
from .state_store import StateUnavailable, StateVersionConflict, WorkstreamRuntimeRecord

BUDGET_WORKSTREAM = "TASK-BUDGET-ACCOUNTING"


def record_confirmed_generation(
    store: Any,
    *,
    project: str,
    route: str,
    operation_key: str,
    generation_transition_key: str,
) -> dict[str, Any]:
    """Count a provider generation exactly once after authoritative confirmation.

    This cumulative counter is historical audit/idempotency evidence only. It is
    never current provider quota-window usage and MUST NOT reduce current-window
    capacity. Current capacity comes from authoritative provider-window
    observation in the runtime budget preflight.
    """

    operation_key = str(operation_key or "").strip()
    transition_key = str(generation_transition_key or "").strip()
    if not operation_key or not transition_key:
        raise ValueError("operation_key and generation_transition_key are required")
    lane_id = canonical_lane_id(project, route, BUDGET_WORKSTREAM)

    for attempt in range(3):
        read = store.read_workstream(lane_id)
        if read.status == "MISSING":
            record = WorkstreamRuntimeRecord(
                lane_id=lane_id,
                project=project,
                route=route,
                workstream_id=BUDGET_WORKSTREAM,
                activation_mode="SHADOW",
            )
            expected = 0
            evidence: dict[str, Any] = {}
        elif read.status == "OK" and read.record is not None:
            record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
            expected = read.version
            evidence = dict(record.evidence_bindings or {})
        else:
            raise StateUnavailable(read.reason or "task budget accounting state unavailable")

        seen = [str(item) for item in evidence.get("confirmed_generation_operation_keys") or [] if str(item)]
        transitions = [str(item) for item in evidence.get("confirmed_generation_transition_keys") or [] if str(item)]
        count = int(evidence.get("ues_confirmed_generation_count") or 0)
        if operation_key in seen or transition_key in transitions:
            return {
                "status": "IDEMPOTENT_GENERATION_ALREADY_ACCOUNTED",
                "ues_confirmed_generation_count": count,
                "operation_key": operation_key,
                "generation_transition_key": transition_key,
                "historical_audit_only": True,
                "capacity_gate_consumption": False,
                "version": read.version,
            }

        record.activation_mode = "SHADOW"
        record.actor_bindings = {}
        record.authority_provenance = {
            "scope": "UES_CONFIRMED_PROVIDER_GENERATION_ACCOUNTING",
            "complete_lifetime_usage_proven": False,
            "counter_is_durable_lower_bound_only": True,
            "historical_audit_only": True,
            "capacity_gate_consumption": False,
        }
        record.evidence_bindings = {
            **evidence,
            "ues_confirmed_generation_count": count + 1,
            "confirmed_generation_operation_keys": (seen + [operation_key])[-512:],
            "confirmed_generation_transition_keys": (transitions + [transition_key])[-512:],
            "complete_lifetime_usage_proven": False,
            "historical_audit_only": True,
            "capacity_gate_consumption": False,
        }
        record.last_successful_transition = {
            "kind": "CONFIRMED_GENERATION_ACCOUNTED",
            "operation_key": operation_key,
            "generation_transition_key": transition_key,
        }
        try:
            saved = store.compare_and_swap_workstream(lane_id, expected, record)
        except StateVersionConflict:
            if attempt < 2:
                continue
            raise
        if saved.status != "OK" or saved.record is None:
            raise StateUnavailable(saved.reason or "failed to persist generation accounting")
        observed = saved.record.evidence_bindings or {}
        if int(observed.get("ues_confirmed_generation_count") or 0) != count + 1:
            raise StateUnavailable("generation accounting post-condition not observed")
        return {
            "status": "GENERATION_ACCOUNTED",
            "ues_confirmed_generation_count": count + 1,
            "operation_key": operation_key,
            "generation_transition_key": transition_key,
            "historical_audit_only": True,
            "capacity_gate_consumption": False,
            "version": saved.version,
        }
    raise StateUnavailable("generation accounting exhausted CAS attempts")


def read_budget_accounting(store: Any, *, project: str, route: str) -> dict[str, Any]:
    lane_id = canonical_lane_id(project, route, BUDGET_WORKSTREAM)
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        return {
            "ues_confirmed_generation_count": 0,
            "complete_lifetime_usage_proven": False,
            "historical_audit_only": True,
            "capacity_gate_consumption": False,
            "status": read.status,
        }
    evidence = read.record.evidence_bindings or {}
    return {
        "ues_confirmed_generation_count": int(evidence.get("ues_confirmed_generation_count") or 0),
        "complete_lifetime_usage_proven": False,
        "historical_audit_only": True,
        "capacity_gate_consumption": False,
        "status": "OK",
    }
