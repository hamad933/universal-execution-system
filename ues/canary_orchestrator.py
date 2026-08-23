from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Protocol

from .idempotency import (
    canonical_request_digest,
    effect_operation_key,
    waiting_answer_effect_identity,
)
from .identity import canonical_lane_id
from .operation_records import sanitize_receipt
from .providers.base import ProviderError, WriteOutcomeUnknown
from .state_store import (
    StateStore,
    claim_operation,
    record_authoritative_readback,
    record_unknown_write,
)

SCHEMA_VERSION = "1.0"
WAITING_ANSWER_ACTION = "waiting-answer"
EXPLICIT_PROOF_STATES = frozenset({"PROVEN_EXPLICIT", "PROVEN_EXPLICIT_SOURCE"})
DELIVERED_OUTCOMES = frozenset({"DELIVERED", "DELIVERED_AFTER_AMBIGUOUS_WRITE"})


class JulesWaitingAnswerClient(Protocol):
    def send_message(
        self,
        session: str,
        prompt: str,
        *,
        expected_repository: str | tuple[str, str] | None = None,
        expected_source: str | None = None,
    ) -> dict[str, Any]: ...


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _deny(
    decision: str,
    *,
    reason: str | None = None,
    operation_key: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "decision": decision,
        "mutation_allowed": False,
        "provider_write_attempted": False,
        "external_effects_dispatched": 0,
        "tasks_or_sessions_created": 0,
        "safe_to_blind_retry": False,
    }
    if reason:
        result["reason"] = reason
    if operation_key:
        result["operation_key"] = operation_key
    return result


def _writer_binding_failure(
    actor_bindings: Mapping[str, Any],
    *,
    session_id: str,
    expected_repository: str,
    expected_source: str,
) -> str | None:
    writer = actor_bindings.get("WRITER")
    if not isinstance(writer, Mapping):
        return "WRITER_BINDING_REQUIRED"
    if str(writer.get("provider") or "").strip().lower() != "jules":
        return "WRITER_PROVIDER_MISMATCH"
    if str(writer.get("session_id") or "").strip() != session_id:
        return "WRITER_SESSION_MISMATCH"
    if str(writer.get("proof_status") or "").strip().upper() not in EXPLICIT_PROOF_STATES:
        return "WRITER_BINDING_NOT_EXPLICITLY_PROVEN"
    source_repository = str(writer.get("source_repository") or "").strip()
    if not source_repository or source_repository.casefold() != expected_repository.casefold():
        return "WRITER_SOURCE_REPOSITORY_MISMATCH"
    source_identity = str(writer.get("source_identity") or "").strip()
    if not source_identity or source_identity != expected_source:
        return "WRITER_SOURCE_IDENTITY_MISMATCH"
    return None


def execute_waiting_answer_canary(
    store: StateStore,
    jules: JulesWaitingAnswerClient,
    *,
    lane_id: str,
    project: str,
    route: str,
    workstream_id: str,
    session_id: str,
    waiting_activity_id: str,
    expected_repository: str,
    expected_source: str,
    prompt: str,
    project_action_authorized: bool,
    canary_authority_event_id: str,
    observed_start: Mapping[str, Any],
    owner: str,
    ttl_seconds: int = 60,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Execute at most one explicitly-authorized existing-session waiting answer.

    This is canary-ready plumbing, not canary authority. It has no task/session creation
    path. Runtime CANARY mode alone is insufficient: project action authorization,
    canonical lane identity, an exact one-shot CanaryGrant and a role-specific explicit
    Writer/source binding are required. JulesClient then independently re-verifies the
    live source/repository before its POST.

    Durable claim/lease/IN_FLIGHT state is persisted before the provider call. Any
    provider ambiguity is recorded as UNKNOWN and never retried blindly.
    """

    lane_id = _required(lane_id, "lane_id")
    project = _required(project, "project")
    route = _required(route, "route")
    workstream_id = _required(workstream_id, "workstream_id")
    session_id = _required(session_id, "session_id")
    waiting_activity_id = _required(waiting_activity_id, "waiting_activity_id")
    expected_repository = _required(expected_repository, "expected_repository")
    expected_source = _required(expected_source, "expected_source")
    prompt = _required(prompt, "prompt")
    canary_authority_event_id = _required(
        canary_authority_event_id, "canary_authority_event_id"
    )
    owner = _required(owner, "owner")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    if lane_id != canonical_lane_id(project, route, workstream_id):
        return _deny("NONCANONICAL_LANE_ID")
    if not project_action_authorized:
        return _deny("PROJECT_ACTION_POLICY_DENIED")

    runtime = store.read_workstream(lane_id)
    if runtime.status != "OK" or runtime.record is None:
        return _deny(
            "RUNTIME_STATE_UNAVAILABLE",
            reason=runtime.reason or runtime.status,
        )
    record = runtime.record
    if (
        record.lane_id != lane_id
        or record.project != project
        or record.route != route
        or record.workstream_id != workstream_id
    ):
        return _deny("RUNTIME_LANE_IDENTITY_MISMATCH")

    binding_failure = _writer_binding_failure(
        record.actor_bindings,
        session_id=session_id,
        expected_repository=expected_repository,
        expected_source=expected_source,
    )
    if binding_failure:
        return _deny(binding_failure)

    effect = waiting_answer_effect_identity(
        lane_id=lane_id,
        project=project,
        route=route,
        workstream_id=workstream_id,
        session_id=session_id,
        waiting_activity_id=waiting_activity_id,
    )
    operation_key = effect_operation_key(effect)
    request_digest = canonical_request_digest({"prompt": prompt})

    claim = claim_operation(
        store,
        lane_id=lane_id,
        owner=owner,
        operation_key=operation_key,
        action=WAITING_ANSWER_ACTION,
        request_digest=request_digest,
        ttl_seconds=ttl_seconds,
        effect_identity=effect,
        observed_start=observed_start,
        canary_authority_event_id=canary_authority_event_id,
        now=now,
    )
    if claim.get("decision") != "CLAIMED" or not claim.get("mutation_allowed"):
        result = _deny(
            str(claim.get("decision") or "CLAIM_DENIED"),
            operation_key=operation_key,
        )
        result["claim"] = sanitize_receipt(claim)
        return result

    try:
        provider_receipt = jules.send_message(
            session_id,
            prompt,
            expected_repository=expected_repository,
            expected_source=expected_source,
        )
    except WriteOutcomeUnknown as exc:
        evidence = sanitize_receipt(exc.to_dict())
        saved = record_unknown_write(
            store,
            lane_id=lane_id,
            operation_key=operation_key,
            result=evidence,
            now=now,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "decision": "WRITE_OUTCOME_UNKNOWN_RECONCILIATION_REQUIRED",
            "mutation_allowed": False,
            "provider_write_attempted": True,
            "external_effects_dispatched": 1,
            "tasks_or_sessions_created": 0,
            "safe_to_blind_retry": False,
            "operation_key": operation_key,
            "operation_state": saved.record.state if saved.record else "UNKNOWN",
            "stop_gate": "AUTHORITATIVE_READBACK_REQUIRED",
            "provider_evidence": evidence,
        }
    except ProviderError as exc:
        evidence = sanitize_receipt(exc.to_dict())
        saved = record_unknown_write(
            store,
            lane_id=lane_id,
            operation_key=operation_key,
            result=evidence,
            now=now,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "decision": "PROVIDER_ERROR_RECONCILIATION_REQUIRED",
            "mutation_allowed": False,
            "provider_write_attempted": True,
            "external_effects_dispatched": 1,
            "tasks_or_sessions_created": 0,
            "safe_to_blind_retry": False,
            "operation_key": operation_key,
            "operation_state": saved.record.state if saved.record else "UNKNOWN",
            "stop_gate": "AUTHORITATIVE_READBACK_REQUIRED",
            "provider_evidence": evidence,
        }

    safe_provider_receipt = sanitize_receipt(provider_receipt)
    if (
        str(safe_provider_receipt.get("outcome") or "") not in DELIVERED_OUTCOMES
        or safe_provider_receipt.get("safe_to_blind_retry") is not False
        or not safe_provider_receipt.get("activity")
    ):
        saved = record_unknown_write(
            store,
            lane_id=lane_id,
            operation_key=operation_key,
            result={
                "category": "MALFORMED_PROVIDER_RECEIPT",
                "receipt": safe_provider_receipt,
            },
            now=now,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "decision": "PROVIDER_RECEIPT_UNPROVEN_RECONCILIATION_REQUIRED",
            "mutation_allowed": False,
            "provider_write_attempted": True,
            "external_effects_dispatched": 1,
            "tasks_or_sessions_created": 0,
            "safe_to_blind_retry": False,
            "operation_key": operation_key,
            "operation_state": saved.record.state if saved.record else "UNKNOWN",
            "stop_gate": "AUTHORITATIVE_READBACK_REQUIRED",
        }

    confirmed = record_authoritative_readback(
        store,
        lane_id=lane_id,
        operation_key=operation_key,
        observed=True,
        evidence=safe_provider_receipt,
        now=now,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "decision": "CANARY_EFFECT_CONFIRMED",
        "mutation_allowed": False,
        "provider_write_attempted": True,
        "external_effects_dispatched": 1,
        "tasks_or_sessions_created": 0,
        "safe_to_blind_retry": False,
        "operation_key": operation_key,
        "operation_state": confirmed.record.state if confirmed.record else "CONFIRMED",
        "provider_receipt": safe_provider_receipt,
    }


def reconcile_waiting_answer_operation(
    store: StateStore,
    *,
    lane_id: str,
    operation_key: str,
    observed: bool | None,
    evidence: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist later authoritative readback without retrying the provider effect."""

    lane_id = _required(lane_id, "lane_id")
    operation_key = _required(operation_key, "operation_key")
    saved = record_authoritative_readback(
        store,
        lane_id=lane_id,
        operation_key=operation_key,
        observed=observed,
        evidence=sanitize_receipt(dict(evidence)),
        now=now,
    )
    state = saved.record.state if saved.record else "UNKNOWN"
    return {
        "schema_version": SCHEMA_VERSION,
        "decision": "AUTHORITATIVE_READBACK_RECORDED",
        "operation_key": operation_key,
        "operation_state": state,
        "mutation_allowed": False,
        "provider_write_attempted": False,
        "external_effects_dispatched": 0,
        "tasks_or_sessions_created": 0,
        "safe_to_blind_retry": False,
        "stop_gate": "AUTHORITATIVE_READBACK_REQUIRED" if state == "UNKNOWN" else None,
    }
