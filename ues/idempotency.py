from __future__ import annotations

from typing import Any

ACTIVE_STATES = {"PLANNED", "EXECUTING", "UNKNOWN"}
TERMINAL_STATES = {"CONFIRMED", "REJECTED", "CANCELLED"}


def latest_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records:
        operation_id = str(record.get("operation_id") or "")
        if not operation_id:
            continue
        if operation_id not in latest:
            order.append(operation_id)
        latest[operation_id] = record
    return [latest[operation_id] for operation_id in order]


def evaluate_idempotency(
    operation_id: str,
    request_digest: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    matches = [record for record in records if record.get("operation_id") == operation_id]
    if not matches:
        return {
            "schema_version": "0.6",
            "decision": "NEW_OPERATION",
            "operation_id": operation_id,
            "safe_to_execute": True,
            "safe_to_blind_retry": False,
        }

    record = matches[-1]
    if record.get("request_digest") != request_digest:
        return {
            "schema_version": "0.6",
            "decision": "OPERATION_ID_COLLISION",
            "operation_id": operation_id,
            "safe_to_execute": False,
            "safe_to_blind_retry": False,
        }

    state = str(record.get("state") or "UNKNOWN")
    if state == "CONFIRMED":
        decision = "IDEMPOTENT_REPLAY_CONFIRMED"
    elif state in ACTIVE_STATES:
        decision = "RECONCILE_REQUIRED"
    else:
        decision = "TERMINAL_REPLAY_REJECTED"
    return {
        "schema_version": "0.6",
        "decision": decision,
        "operation_id": operation_id,
        "existing_state": state,
        "safe_to_execute": False,
        "safe_to_blind_retry": False,
    }


def branch_serialization_key(repository: str, ref: str) -> str:
    return f"ues-write:{repository}:{ref}"


def evaluate_branch_serialization(
    *,
    repository: str,
    ref: str,
    operation_id: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    conflicts = []
    for record in latest_records(records):
        if record.get("operation_id") == operation_id:
            continue
        if record.get("repository") != repository or record.get("ref") != ref:
            continue
        if str(record.get("state") or "") not in ACTIVE_STATES:
            continue
        conflicts.append({
            "operation_id": record.get("operation_id"),
            "state": record.get("state"),
        })
    return {
        "schema_version": "0.6",
        "serialization_key": branch_serialization_key(repository, ref),
        "available": not conflicts,
        "conflicts": conflicts,
    }


def make_operation_receipt(
    *,
    operation_id: str,
    request_digest: str,
    repository: str,
    ref: str,
    authority_event_id: str,
    start_sha: str,
    start_tree_sha: str | None,
    state: str = "PLANNED",
) -> dict[str, Any]:
    if state not in ACTIVE_STATES | TERMINAL_STATES:
        raise ValueError(f"unsupported operation state: {state}")
    return {
        "schema_version": "0.6",
        "operation_id": operation_id,
        "request_digest": request_digest,
        "repository": repository,
        "ref": ref,
        "authority_event_id": authority_event_id,
        "start_sha": start_sha,
        "start_tree_sha": start_tree_sha,
        "state": state,
        "safe_to_blind_retry": False,
    }


def evaluate_write_boundary(
    *,
    mutation_plan: dict[str, Any],
    operation_id: str,
    request_digest: str,
    repository: str,
    ref: str,
    live_head_sha: str,
    live_tree_sha: str | None,
    operation_records: list[dict[str, Any]],
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    if mutation_plan.get("decision") != "AUTHORIZED_DRY_RUN":
        failures.append({"code": "MUTATION_PLAN_NOT_AUTHORIZED"})

    cas = mutation_plan.get("cas") or {}
    if cas.get("live_head_sha") != live_head_sha:
        failures.append({"code": "WRITE_BOUNDARY_HEAD_MOVED"})
    planned_tree = cas.get("live_tree_sha")
    if planned_tree and live_tree_sha and planned_tree != live_tree_sha:
        failures.append({"code": "WRITE_BOUNDARY_TREE_MOVED"})

    idem = evaluate_idempotency(operation_id, request_digest, operation_records)
    if not idem["safe_to_execute"]:
        failures.append({"code": "IDEMPOTENCY_BLOCK", "decision": idem["decision"]})

    serialization = evaluate_branch_serialization(
        repository=repository,
        ref=ref,
        operation_id=operation_id,
        records=operation_records,
    )
    if not serialization["available"]:
        failures.append({"code": "BRANCH_SERIALIZATION_CONFLICT"})

    ready = not failures
    return {
        "schema_version": "0.6",
        "decision": "READY_FOR_EXECUTOR_INTEGRATION" if ready else "BLOCKED",
        "ready": ready,
        "execution_enabled": False,
        "safe_to_execute_now": False,
        "safe_to_blind_retry": False,
        "operation_id": operation_id,
        "idempotency": idem,
        "serialization": serialization,
        "failures": failures,
    }
