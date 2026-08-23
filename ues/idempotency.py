from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

ACTIVE_STATES = {"PLANNED", "EXECUTING", "IN_FLIGHT", "UNKNOWN"}
TERMINAL_STATES = {"CONFIRMED", "REJECTED", "CANCELLED"}
READBACK_RETRYABLE_STATES = {"RECONCILED_NOT_OBSERVED"}


def latest_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records:
        operation_id = str(record.get("operation_id") or record.get("operation_key") or "")
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
    matches = [
        record
        for record in records
        if (record.get("operation_id") or record.get("operation_key")) == operation_id
    ]
    if not matches:
        return {
            "schema_version": "0.8",
            "decision": "NEW_OPERATION",
            "operation_id": operation_id,
            "safe_to_execute": True,
            "safe_to_blind_retry": False,
        }

    record = matches[-1]
    if record.get("request_digest") != request_digest:
        return {
            "schema_version": "0.8",
            "decision": "OPERATION_ID_COLLISION",
            "operation_id": operation_id,
            "safe_to_execute": False,
            "safe_to_blind_retry": False,
        }

    state = str(record.get("state") or "UNKNOWN")
    if state == "CONFIRMED":
        decision = "IDEMPOTENT_REPLAY_CONFIRMED"
        safe_to_execute = False
    elif state in ACTIVE_STATES:
        decision = "RECONCILE_REQUIRED"
        safe_to_execute = False
    elif state in READBACK_RETRYABLE_STATES:
        decision = "READBACK_CONFIRMED_NOT_OBSERVED"
        safe_to_execute = bool(record.get("authoritative_readback"))
    else:
        decision = "TERMINAL_REPLAY_REJECTED"
        safe_to_execute = False
    return {
        "schema_version": "0.8",
        "decision": decision,
        "operation_id": operation_id,
        "existing_state": state,
        "safe_to_execute": safe_to_execute,
        "safe_to_blind_retry": False,
    }


def canonical_request_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _normalize_target(target: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    normalized: list[tuple[str, str]] = []
    for key, value in sorted(target.items(), key=lambda item: str(item[0])):
        if value is None:
            continue
        normalized.append((str(key), str(value)))
    if not normalized:
        raise ValueError("effect target must contain at least one exact binding")
    return tuple(normalized)


@dataclass(frozen=True)
class EffectIdentity:
    """One canonical external effect, independent of request payload.

    `lane_id` is supplied by Integration Authority and represents the complete
    `(project, route, workstream)` lane. Audit fields are retained explicitly so
    a scalar collision or wrong-lane handoff fails closed rather than becoming
    an implicit authority grant.
    """

    lane_id: str
    project: str
    route: str
    workstream_id: str
    action: str
    target: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        for name in ("lane_id", "project", "route", "workstream_id", "action"):
            _required_text(getattr(self, name), name)
        if not self.target:
            raise ValueError("effect target is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "project": self.project,
            "route": self.route,
            "workstream_id": self.workstream_id,
            "action": self.action,
            "target": dict(self.target),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EffectIdentity":
        target = value.get("target")
        if not isinstance(target, Mapping):
            raise ValueError("effect target must be an object")
        return canonical_effect_identity(
            lane_id=str(value.get("lane_id") or ""),
            project=str(value.get("project") or ""),
            route=str(value.get("route") or ""),
            workstream_id=str(value.get("workstream_id") or ""),
            action=str(value.get("action") or ""),
            target=target,
        )


def canonical_effect_identity(
    *,
    lane_id: str,
    project: str,
    route: str,
    workstream_id: str,
    action: str,
    target: Mapping[str, Any],
) -> EffectIdentity:
    return EffectIdentity(
        lane_id=_required_text(lane_id, "lane_id"),
        project=_required_text(project, "project"),
        route=_required_text(route, "route"),
        workstream_id=_required_text(workstream_id, "workstream_id"),
        action=_required_text(action, "action"),
        target=_normalize_target(target),
    )


def effect_operation_key(effect: EffectIdentity) -> str:
    digest = canonical_request_digest(effect.to_dict())
    safe_action = "".join(
        ch if ch.isalnum() or ch in "-_" else "-" for ch in effect.action.lower()
    )
    return f"ues-v2:{safe_action}:{digest}"


def build_operation_key(
    *,
    lane_id: str,
    action: str,
    project: str,
    route: str,
    workstream_id: str,
    identity: Mapping[str, Any],
) -> str:
    return effect_operation_key(
        canonical_effect_identity(
            lane_id=lane_id,
            action=action,
            project=project,
            route=route,
            workstream_id=workstream_id,
            target=identity,
        )
    )


def waiting_answer_effect_identity(
    *,
    lane_id: str,
    project: str,
    route: str,
    workstream_id: str,
    session_id: str,
    waiting_activity_id: str,
) -> EffectIdentity:
    return canonical_effect_identity(
        lane_id=lane_id,
        action="waiting-answer",
        project=project,
        route=route,
        workstream_id=workstream_id,
        target={
            "provider": "jules",
            "session_id": session_id,
            "waiting_activity_id": waiting_activity_id,
        },
    )


def waiting_answer_operation_key(
    *,
    lane_id: str,
    project: str,
    route: str,
    workstream_id: str,
    session_id: str,
    waiting_activity_id: str,
    answer_digest: str | None = None,
) -> str:
    # Payload digest is request evidence and never effect identity.
    return effect_operation_key(
        waiting_answer_effect_identity(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=workstream_id,
            session_id=session_id,
            waiting_activity_id=waiting_activity_id,
        )
    )


def correction_packet_effect_identity(
    *,
    lane_id: str,
    project: str,
    route: str,
    workstream_id: str,
    writer_session_id: str,
    reviewer_session_id: str,
    candidate_sha: str,
) -> EffectIdentity:
    return canonical_effect_identity(
        lane_id=lane_id,
        action="reviewer-writer-correction",
        project=project,
        route=route,
        workstream_id=workstream_id,
        target={
            "writer_session_id": writer_session_id,
            "reviewer_session_id": reviewer_session_id,
            "candidate_sha": candidate_sha,
        },
    )


def correction_packet_operation_key(
    *,
    lane_id: str,
    project: str,
    route: str,
    workstream_id: str,
    writer_session_id: str,
    reviewer_session_id: str,
    candidate_sha: str,
    findings_digest: str | None = None,
) -> str:
    return effect_operation_key(
        correction_packet_effect_identity(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=workstream_id,
            writer_session_id=writer_session_id,
            reviewer_session_id=reviewer_session_id,
            candidate_sha=candidate_sha,
        )
    )


def reviewer_dispatch_effect_identity(
    *,
    lane_id: str,
    project: str,
    route: str,
    workstream_id: str,
    candidate_sha: str,
    reviewer_lineage: str,
    dispatch_target: str,
    re_review: bool = False,
) -> EffectIdentity:
    return canonical_effect_identity(
        lane_id=lane_id,
        action="reviewer-rereview-dispatch" if re_review else "reviewer-dispatch",
        project=project,
        route=route,
        workstream_id=workstream_id,
        target={
            "candidate_sha": candidate_sha,
            "reviewer_lineage": reviewer_lineage,
            "dispatch_target": dispatch_target,
        },
    )


def reviewer_dispatch_operation_key(
    *,
    lane_id: str,
    project: str,
    route: str,
    workstream_id: str,
    candidate_sha: str,
    reviewer_lineage: str,
    dispatch_target: str,
    re_review: bool = False,
) -> str:
    return effect_operation_key(
        reviewer_dispatch_effect_identity(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=workstream_id,
            candidate_sha=candidate_sha,
            reviewer_lineage=reviewer_lineage,
            dispatch_target=dispatch_target,
            re_review=re_review,
        )
    )


def task_session_effect_identity(
    *,
    lane_id: str,
    project: str,
    route: str,
    workstream_id: str,
    intent: str,
    task_kind: str,
    lineage: str,
    authority_event_id: str,
) -> EffectIdentity:
    if intent not in {"recommendation", "create"}:
        raise ValueError("intent must be 'recommendation' or 'create'")
    return canonical_effect_identity(
        lane_id=lane_id,
        action=f"task-session-{intent}",
        project=project,
        route=route,
        workstream_id=workstream_id,
        target={
            "task_kind": task_kind,
            "lineage": lineage,
            "authority_event_id": authority_event_id,
        },
    )


def task_session_operation_key(
    *,
    lane_id: str,
    project: str,
    route: str,
    workstream_id: str,
    intent: str,
    task_kind: str,
    lineage: str,
    authority_event_id: str,
) -> str:
    return effect_operation_key(
        task_session_effect_identity(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=workstream_id,
            intent=intent,
            task_kind=task_kind,
            lineage=lineage,
            authority_event_id=authority_event_id,
        )
    )


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
        current_id = record.get("operation_id") or record.get("operation_key")
        if current_id == operation_id:
            continue
        if record.get("repository") != repository or record.get("ref") != ref:
            continue
        if str(record.get("state") or "") not in ACTIVE_STATES:
            continue
        conflicts.append({"operation_id": current_id, "state": record.get("state")})
    return {
        "schema_version": "0.8",
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
    if state not in ACTIVE_STATES | TERMINAL_STATES | READBACK_RETRYABLE_STATES:
        raise ValueError(f"unsupported operation state: {state}")
    return {
        "schema_version": "0.8",
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
        "schema_version": "0.8",
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
