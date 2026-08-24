from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from .identity import canonical_lane_id
from .idempotency import canonical_effect_identity, canonical_request_digest, effect_operation_key
from .providers.base import ProviderError, WriteOutcomeUnknown
from .state_store import (
    MutationAuthorization,
    StateUnavailable,
    WorkstreamRuntimeRecord,
    claim_operation,
    record_authoritative_readback,
    record_unknown_write,
)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _inputs_digest(inputs: Mapping[str, str]) -> str:
    normalized = {str(k): str(v) for k, v in sorted(inputs.items())}
    return sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _ensure_effect_lane(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    authority_event_id: str,
) -> str:
    lane_id = canonical_lane_id(project, route, workstream)
    read = store.read_workstream(lane_id)
    if read.status == "MISSING":
        record = WorkstreamRuntimeRecord(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=workstream,
            activation_mode="ACTIVE_AUTO_SAFE",
        )
        expected = 0
    elif read.status == "OK" and read.record is not None:
        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        expected = read.version
        if record.unknown_write_state or record.action_in_flight:
            raise StateUnavailable("workflow dispatch lane has unresolved effect state")
        record.activation_mode = "ACTIVE_AUTO_SAFE"
    else:
        raise StateUnavailable(read.reason or "workflow dispatch lane unavailable")
    record.authority_provenance = {
        **(record.authority_provenance or {}),
        "authority_event_id": authority_event_id,
        "scope": "BOUNDED_GITHUB_WORKFLOW_DISPATCH",
        "effect_scope_active": True,
        "arbitrary_workflow_execution_authorized": False,
    }
    saved = store.compare_and_swap_workstream(lane_id, expected, record)
    if saved.status != "OK":
        raise StateUnavailable(saved.reason or "failed to open workflow dispatch effect lane")
    return lane_id


def _restore_shadow(store: Any, lane_id: str, *, authority_event_id: str) -> None:
    for _ in range(3):
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or "workflow dispatch lane unavailable during SHADOW restore")
        if read.record.activation_mode == "SHADOW":
            return
        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        record.activation_mode = "SHADOW"
        record.authority_provenance = {
            **(record.authority_provenance or {}),
            "last_effect_authority_event_id": authority_event_id,
            "effect_scope_active": False,
        }
        try:
            saved = store.compare_and_swap_workstream(lane_id, read.version, record)
        except Exception:
            continue
        if saved.status == "OK" and saved.record is not None and saved.record.activation_mode == "SHADOW":
            return
    raise StateUnavailable("workflow dispatch lane SHADOW restoration failed")


def dispatch_workflow_once(
    store: Any,
    github: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    owner: str,
    repo: str,
    workflow: str,
    ref: str,
    expected_sha: str,
    inputs: Mapping[str, str],
    allowed_workflows: Sequence[str],
    allowed_inputs: Mapping[str, Sequence[str]],
    purpose: str,
    authority_event_id: str,
) -> dict[str, Any]:
    """Durably claim and execute one exact bounded workflow_dispatch effect."""

    normalized_inputs = {str(k): str(v) for k, v in sorted(inputs.items())}
    input_digest = _inputs_digest(normalized_inputs)
    lane_id = _ensure_effect_lane(
        store,
        project=project,
        route=route,
        workstream=workstream,
        authority_event_id=authority_event_id,
    )
    try:
        effect = canonical_effect_identity(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=workstream,
            action="github-workflow-dispatch",
            target={
                "repository": f"{owner}/{repo}",
                "workflow": workflow,
                "ref": ref,
                "expected_sha": expected_sha.lower(),
                "inputs_digest": input_digest,
                "purpose": purpose,
            },
        )
        operation_key = effect_operation_key(effect)
        request_digest = canonical_request_digest(
            {
                "workflow": workflow,
                "ref": ref,
                "expected_sha": expected_sha.lower(),
                "inputs": normalized_inputs,
                "purpose": purpose,
            }
        )
        authorization = MutationAuthorization(
            effect_identity=effect,
            authority_event_id=authority_event_id,
            project_policy_authorized=True,
            exact_binding_proven=True,
            evidence_verified=True,
            expires_at=_iso(datetime.now(timezone.utc) + timedelta(minutes=10)),
        )
        claim = claim_operation(
            store,
            lane_id=lane_id,
            owner="ues-workflow-dispatch",
            operation_key=operation_key,
            action="github-workflow-dispatch",
            request_digest=request_digest,
            ttl_seconds=180,
            receipt={
                "repository": f"{owner}/{repo}",
                "workflow": workflow,
                "ref": ref,
                "expected_sha": expected_sha.lower(),
                "inputs_digest": input_digest,
                "purpose": purpose,
            },
            effect_identity=effect,
            authorization=authorization,
            observed_start={"expected_sha": expected_sha.lower()},
        )
        if claim.get("decision") != "CLAIMED" or not claim.get("mutation_allowed"):
            return {
                "decision": str(claim.get("decision") or "CLAIM_DENIED"),
                "provider_write_attempted": False,
                "operation_key": operation_key,
                "safe_to_blind_retry": False,
            }

        try:
            receipt = github.dispatch_workflow_bounded(
                owner,
                repo,
                workflow=workflow,
                ref=ref,
                expected_sha=expected_sha,
                inputs=normalized_inputs,
                allowed_workflows=allowed_workflows,
                allowed_inputs=allowed_inputs,
                purpose=purpose,
            )
        except WriteOutcomeUnknown as exc:
            recovery = exc.to_dict().get("recovery") if hasattr(exc, "to_dict") else {}
            record_unknown_write(
                store,
                lane_id=lane_id,
                operation_key=operation_key,
                result={
                    "category": "WORKFLOW_DISPATCH_OUTCOME_UNKNOWN",
                    "recovery": recovery,
                    "safe_to_blind_retry": False,
                },
            )
            return {
                "decision": "WORKFLOW_DISPATCH_OUTCOME_UNKNOWN_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
                "recovery": recovery,
                "safe_to_blind_retry": False,
            }
        except ProviderError as exc:
            record_unknown_write(
                store,
                lane_id=lane_id,
                operation_key=operation_key,
                result={"category": getattr(exc, "category", type(exc).__name__), "safe_to_blind_retry": False},
            )
            return {
                "decision": "WORKFLOW_DISPATCH_PROVIDER_ERROR_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
                "safe_to_blind_retry": False,
            }

        evidence = {
            "repository": receipt.get("repository"),
            "workflow": receipt.get("workflow"),
            "ref": receipt.get("ref"),
            "head_sha": receipt.get("head_sha"),
            "run_id": receipt.get("run_id"),
            "run_attempt": receipt.get("run_attempt"),
            "event": receipt.get("event"),
            "inputs_digest": input_digest,
            "purpose": purpose,
            "authoritative_readback": bool(receipt.get("authoritative_readback")),
            "safe_to_blind_retry": False,
        }
        confirmed = record_authoritative_readback(
            store,
            lane_id=lane_id,
            operation_key=operation_key,
            observed=True,
            evidence=evidence,
        )
        return {
            "decision": "WORKFLOW_DISPATCH_CONFIRMED",
            "provider_write_attempted": True,
            "external_effects_dispatched": 1,
            "operation_key": operation_key,
            "operation_state": confirmed.record.state if confirmed.record else "CONFIRMED",
            **evidence,
        }
    finally:
        _restore_shadow(store, lane_id, authority_event_id=authority_event_id)


def reconcile_unknown_workflow_dispatch(
    store: Any,
    github: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    owner: str,
    repo: str,
    workflow: str,
    ref: str,
    expected_sha: str,
    inputs: Mapping[str, str],
    purpose: str,
) -> dict[str, Any]:
    """Authoritatively reconcile an UNKNOWN dispatch without another POST."""

    lane_id = canonical_lane_id(project, route, workstream)
    ws = store.read_workstream(lane_id)
    if ws.status != "OK" or ws.record is None or not isinstance(ws.record.unknown_write_state, Mapping):
        raise StateUnavailable("workflow dispatch reconciliation requires durable UNKNOWN state")
    operation_key = str(ws.record.unknown_write_state.get("operation_key") or "").strip()
    op_read = store.read_operation(operation_key)
    if op_read.status != "OK" or op_read.record is None:
        raise StateUnavailable("workflow dispatch operation record is unavailable")
    op = op_read.record
    if op.state not in {"UNKNOWN", "IN_FLIGHT"}:
        return {
            "decision": "WORKFLOW_DISPATCH_ALREADY_RECONCILED",
            "operation_key": operation_key,
            "operation_state": op.state,
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }

    expected_target = {
        "repository": f"{owner}/{repo}",
        "workflow": workflow,
        "ref": ref,
        "expected_sha": expected_sha.lower(),
        "inputs_digest": _inputs_digest(inputs),
        "purpose": purpose,
    }
    effect = op.effect_identity if isinstance(op.effect_identity, Mapping) else {}
    actual_target = effect.get("target") if isinstance(effect.get("target"), Mapping) else {}
    if dict(actual_target) != expected_target:
        raise StateUnavailable("workflow dispatch reconciliation binding does not match UNKNOWN operation")

    result = op.receipt.get("result") if isinstance(op.receipt, Mapping) else None
    result = result if isinstance(result, Mapping) else {}
    recovery = result.get("recovery") if isinstance(result.get("recovery"), Mapping) else {}
    before_ids = recovery.get("before_run_ids") if isinstance(recovery.get("before_run_ids"), list) else None
    if before_ids is None:
        raise StateUnavailable("workflow dispatch UNKNOWN state lacks pre-dispatch run identity set")

    reconciled = github.reconcile_workflow_dispatch_bounded(
        owner,
        repo,
        workflow=workflow,
        ref=ref,
        expected_sha=expected_sha,
        before_run_ids=[int(item) for item in before_ids],
    )
    if reconciled.get("decision") != "WORKFLOW_DISPATCH_AUTHORITATIVELY_RECONCILED":
        return {
            **reconciled,
            "operation_key": operation_key,
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }

    evidence = {
        "repository": f"{owner}/{repo}",
        "workflow": workflow,
        "ref": ref,
        "head_sha": expected_sha,
        "run_id": reconciled.get("run_id"),
        "run_attempt": reconciled.get("run_attempt"),
        "event": "workflow_dispatch",
        "inputs_digest": _inputs_digest(inputs),
        "purpose": purpose,
        "authoritative_reconciliation": True,
        "safe_to_blind_retry": False,
    }
    confirmed = record_authoritative_readback(
        store,
        lane_id=lane_id,
        operation_key=operation_key,
        observed=True,
        evidence=evidence,
    )
    return {
        "decision": "WORKFLOW_DISPATCH_UNKNOWN_AUTHORITATIVELY_RECONCILED",
        "operation_key": operation_key,
        "operation_state": confirmed.record.state if confirmed.record else "CONFIRMED",
        "provider_write_attempted": False,
        **evidence,
    }
