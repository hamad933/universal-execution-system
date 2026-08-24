from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Mapping

from .idempotency import canonical_effect_identity, canonical_request_digest, effect_operation_key
from .jules_lifecycle import JulesLifecycleClient
from .lineage_registry import lineage_lane_id, session_fingerprint
from .providers.base import ProviderError, WriteOutcomeUnknown
from .state_store import (
    MutationAuthorization,
    StateUnavailable,
    StateVersionConflict,
    WorkstreamRuntimeRecord,
    claim_operation,
    record_authoritative_readback,
    record_unknown_write,
)

DEFAULT_TTL_SECONDS = 180


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _authorize_lane(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    authority_event_id: str,
    scope: str,
) -> str:
    """Open only the exact effect boundary immediately before a durable claim."""

    lane_id = lineage_lane_id(project, route, workstream, role)
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable(read.reason or f"lineage lane unavailable: {lane_id}")
    record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
    record.activation_mode = "ACTIVE_AUTO_SAFE"
    record.authority_provenance = {
        **(record.authority_provenance or {}),
        "authority_event_id": authority_event_id,
        "effect_scope": scope,
        "effect_scope_active": True,
        "new_logical_lineage_authorized": False,
        "merge_release_deploy_authorized": False,
    }
    saved = store.compare_and_swap_workstream(lane_id, read.version, record)
    if saved.status != "OK":
        raise StateUnavailable(saved.reason or "failed to authorize exact lineage lane")
    return lane_id


def _restore_lane_shadow(
    store: Any,
    lane_id: str,
    *,
    authority_event_id: str,
) -> None:
    """Close the transient effect boundary even after denial, failure, or UNKNOWN.

    This is a state-only reconciliation. It never retries a provider effect.
    A short CAS retry is safe because every attempt re-reads authoritative lane
    state and only narrows activation back to SHADOW.
    """

    for attempt in range(3):
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or f"lineage lane unavailable while restoring SHADOW: {lane_id}")
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
        except StateVersionConflict:
            if attempt < 2:
                continue
            raise
        if saved.status != "OK" or saved.record is None:
            raise StateUnavailable(saved.reason or "failed to restore exact lineage lane to SHADOW")
        if saved.record.activation_mode != "SHADOW":
            raise StateUnavailable("lineage lane SHADOW post-condition was not observed")
        return
    raise StateUnavailable("lineage lane SHADOW restoration exhausted CAS attempts")


def _record_unexpected_provider_error(
    store: Any,
    *,
    lane_id: str,
    operation_key: str,
) -> None:
    record_unknown_write(
        store,
        lane_id=lane_id,
        operation_key=operation_key,
        result={"category": "UNEXPECTED_PROVIDER_ERROR", "safe_to_blind_retry": False},
    )


def send_same_lineage_message(
    store: Any,
    client: JulesLifecycleClient,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    session_name: str,
    source_name: str,
    repository: str,
    prompt: str,
    trigger_fingerprint: str,
    authority_event_id: str,
    action: str = "lineage-message",
) -> dict[str, Any]:
    if not prompt.strip():
        raise ValueError("prompt is required")
    if not trigger_fingerprint.strip():
        raise ValueError("trigger_fingerprint is required")

    lane_id = _authorize_lane(
        store,
        project=project,
        route=route,
        workstream=workstream,
        role=role,
        authority_event_id=authority_event_id,
        scope="SAME_LOGICAL_LINEAGE_EXISTING_SESSION_MESSAGE",
    )
    try:
        session_fp = session_fingerprint(session_name)
        effect = canonical_effect_identity(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=f"LINEAGE::{workstream}::{role.upper()}",
            action=action,
            target={
                "provider": "jules",
                "role": role.upper(),
                "session_fingerprint": session_fp,
                "trigger_fingerprint": trigger_fingerprint,
            },
        )
        operation_key = effect_operation_key(effect)
        request_digest = canonical_request_digest({"prompt": prompt})
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
            owner="ues-lineage-lifecycle",
            operation_key=operation_key,
            action=action,
            request_digest=request_digest,
            ttl_seconds=DEFAULT_TTL_SECONDS,
            receipt={
                "session_fingerprint": session_fp,
                "trigger_fingerprint": trigger_fingerprint,
                "role": role.upper(),
                "raw_session_id_persisted": False,
            },
            effect_identity=effect,
            authorization=authorization,
            observed_start={"session_fingerprint": session_fp},
        )
        if claim.get("decision") != "CLAIMED" or not claim.get("mutation_allowed"):
            return {
                "decision": str(claim.get("decision") or "CLAIM_DENIED"),
                "provider_write_attempted": False,
                "operation_key": operation_key,
                "safe_to_blind_retry": False,
            }

        try:
            receipt = client.send_message(
                session_name,
                prompt,
                expected_repository=repository,
                expected_source=source_name,
            )
        except WriteOutcomeUnknown as exc:
            recovery = exc.to_dict().get("recovery") if hasattr(exc, "to_dict") else {}
            record_unknown_write(
                store,
                lane_id=lane_id,
                operation_key=operation_key,
                result={
                    "category": "WRITE_OUTCOME_UNKNOWN",
                    "recovery_verdict": recovery.get("verdict") if isinstance(recovery, Mapping) else None,
                    "safe_to_blind_retry": False,
                },
            )
            return {
                "decision": "WRITE_OUTCOME_UNKNOWN_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
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
                "decision": "PROVIDER_ERROR_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
                "safe_to_blind_retry": False,
            }
        except Exception:
            _record_unexpected_provider_error(store, lane_id=lane_id, operation_key=operation_key)
            return {
                "decision": "UNEXPECTED_PROVIDER_ERROR_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
                "safe_to_blind_retry": False,
            }

        activity = receipt.get("activity")
        evidence = {
            "outcome": receipt.get("outcome"),
            "repository": receipt.get("repository"),
            "activity_fingerprint": sha256(str(activity or "").encode("utf-8")).hexdigest() if activity else None,
            "session_fingerprint": session_fp,
            "trigger_fingerprint": trigger_fingerprint,
            "raw_session_id_persisted": False,
            "raw_activity_id_persisted": False,
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
            "decision": "SAME_LINEAGE_MESSAGE_CONFIRMED",
            "provider_write_attempted": True,
            "external_effects_dispatched": 1,
            "operation_key": operation_key,
            "operation_state": confirmed.record.state if confirmed.record else "CONFIRMED",
            "session_fingerprint": session_fp,
            "safe_to_blind_retry": False,
        }
    finally:
        _restore_lane_shadow(store, lane_id, authority_event_id=authority_event_id)


def create_next_lineage_generation(
    store: Any,
    client: JulesLifecycleClient,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    predecessor_session_fingerprint: str | None,
    next_generation: int,
    prompt: str,
    title: str,
    source_name: str,
    starting_branch: str,
    repository: str,
    authority_event_id: str,
    budget_safe: bool,
) -> dict[str, Any]:
    if not budget_safe:
        return {
            "decision": "NEW_SESSION_BUDGET_NOT_PROVEN",
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }

    lane_id = _authorize_lane(
        store,
        project=project,
        route=route,
        workstream=workstream,
        role=role,
        authority_event_id=authority_event_id,
        scope="NEXT_SESSION_GENERATION_SAME_LOGICAL_LINEAGE",
    )
    try:
        source_fp = sha256(source_name.encode("utf-8")).hexdigest()
        effect = canonical_effect_identity(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=f"LINEAGE::{workstream}::{role.upper()}",
            action="create-session-generation",
            target={
                "provider": "jules",
                "role": role.upper(),
                "generation": str(next_generation),
                "predecessor_session_fingerprint": predecessor_session_fingerprint or "NONE",
                "source_fingerprint": source_fp,
                "starting_branch": starting_branch,
            },
        )
        operation_key = effect_operation_key(effect)
        request_digest = canonical_request_digest({"prompt": prompt, "title": title})
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
            owner="ues-lineage-lifecycle",
            operation_key=operation_key,
            action="create-session-generation",
            request_digest=request_digest,
            ttl_seconds=DEFAULT_TTL_SECONDS,
            receipt={
                "generation": next_generation,
                "predecessor_session_fingerprint": predecessor_session_fingerprint,
                "source_fingerprint": source_fp,
                "starting_branch": starting_branch,
                "raw_session_id_persisted": False,
            },
            effect_identity=effect,
            authorization=authorization,
            observed_start={"generation": str(next_generation - 1)},
        )
        if claim.get("decision") != "CLAIMED" or not claim.get("mutation_allowed"):
            return {
                "decision": str(claim.get("decision") or "CLAIM_DENIED"),
                "provider_write_attempted": False,
                "operation_key": operation_key,
                "safe_to_blind_retry": False,
            }

        try:
            receipt = client.create_session(
                prompt=prompt,
                title=title,
                source=source_name,
                starting_branch=starting_branch,
                expected_repository=repository,
            )
        except WriteOutcomeUnknown as exc:
            recovery = exc.to_dict().get("recovery") if hasattr(exc, "to_dict") else {}
            record_unknown_write(
                store,
                lane_id=lane_id,
                operation_key=operation_key,
                result={
                    "category": "WRITE_OUTCOME_UNKNOWN",
                    "recovery_verdict": recovery.get("verdict") if isinstance(recovery, Mapping) else None,
                    "safe_to_blind_retry": False,
                },
            )
            return {
                "decision": "CREATE_SESSION_OUTCOME_UNKNOWN_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
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
                "decision": "CREATE_SESSION_PROVIDER_ERROR_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
                "safe_to_blind_retry": False,
            }
        except Exception:
            _record_unexpected_provider_error(store, lane_id=lane_id, operation_key=operation_key)
            return {
                "decision": "CREATE_SESSION_UNEXPECTED_PROVIDER_ERROR_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
                "safe_to_blind_retry": False,
            }

        new_fp = session_fingerprint(receipt.get("session"))
        evidence = {
            "outcome": "SESSION_CREATED",
            "session_fingerprint": new_fp,
            "repository": receipt.get("repository"),
            "starting_branch": receipt.get("starting_branch"),
            "generation": next_generation,
            "raw_session_id_persisted": False,
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
            "decision": "NEXT_SESSION_GENERATION_CONFIRMED",
            "provider_write_attempted": True,
            "external_effects_dispatched": 1,
            "new_tasks_or_sessions_created": 1,
            "operation_key": operation_key,
            "operation_state": confirmed.record.state if confirmed.record else "CONFIRMED",
            "session_fingerprint": new_fp,
            "safe_to_blind_retry": False,
        }
    finally:
        _restore_lane_shadow(store, lane_id, authority_event_id=authority_event_id)
