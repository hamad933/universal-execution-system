from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Mapping

from .current_authority import initial_lineage_authority
from .generation_transition import assess_initial_lineage_creation
from .idempotency import canonical_effect_identity, canonical_request_digest, effect_operation_key
from .lineage_effects import DEFAULT_TTL_SECONDS, _authorize_lane, _restore_lane_shadow
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
from .task_budget_accounting import record_confirmed_generation


INITIAL_SCOPE = "INITIAL_LOGICAL_LINEAGE_CREATE"
INITIAL_ACTION = "create-initial-lineage-session"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _state_role(role: str) -> str:
    return "ASSURANCE" if str(role).upper() == "FINAL_ASSURANCE" else str(role).upper()


def _ensure_initial_lane(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    candidate_sha: str | None,
) -> str:
    lane_id = lineage_lane_id(project, route, workstream, _state_role(role))
    for attempt in range(3):
        read = store.read_workstream(lane_id)
        if read.status == "OK" and read.record is not None:
            return lane_id
        if read.status != "MISSING":
            raise StateUnavailable(read.reason or "initial lineage lane unavailable")
        record = WorkstreamRuntimeRecord(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=f"LINEAGE::{workstream}::{_state_role(role)}",
            activation_mode="SHADOW",
            authority_provenance={
                "scope": "INITIAL_LOGICAL_LINEAGE_PENDING",
                "effect_scope_active": False,
                "provider_mutation_authorized": False,
            },
            evidence_bindings={
                "schema_version": "1.0",
                "role": _state_role(role),
                "workstream": workstream,
                "generation": 0,
                "session_fingerprint": None,
                "current_candidate_sha": candidate_sha,
                "binding_status": "UNBOUND",
                "raw_session_id_persisted": False,
            },
        )
        try:
            saved = store.compare_and_swap_workstream(lane_id, 0, record)
        except StateVersionConflict:
            if attempt < 2:
                continue
            raise
        if saved.status == "OK":
            return lane_id
    raise StateUnavailable("initial lineage lane initialization exhausted CAS attempts")


def _snapshot(store: Any, lane_id: str) -> dict[str, Any]:
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable(read.reason or "initial lineage lane missing")
    evidence = read.record.evidence_bindings or {}
    return {
        "version": read.version,
        "generation": int(evidence.get("generation") or 0),
        "session_fingerprint": str(evidence.get("session_fingerprint") or "").strip() or None,
        "initial_lineage_transition_key": str(evidence.get("initial_lineage_transition_key") or "").strip() or None,
        "pending": evidence.get("pending_initial_lineage_transition"),
        "unknown_write_state": read.record.unknown_write_state,
        "action_in_flight": read.record.action_in_flight,
    }


def _persist_pending_initial(
    store: Any,
    *,
    lane_id: str,
    transition: Mapping[str, Any],
    repository: str,
    source_name: str,
    starting_branch: str,
    candidate_sha: str | None,
    task_spec_digest: str,
) -> dict[str, Any]:
    transition_key = str(transition.get("transition_key") or "").strip()
    if not transition_key:
        raise ValueError("initial transition key is required")
    for attempt in range(3):
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or "initial lineage lane unavailable before pending transition")
        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        evidence = dict(record.evidence_bindings or {})
        existing = evidence.get("pending_initial_lineage_transition")
        if isinstance(existing, Mapping):
            existing_key = str(existing.get("transition_key") or "")
            if existing_key == transition_key:
                return {"status": "IDEMPOTENT_PENDING_INITIAL_PRESENT", "version": read.version}
            if record.unknown_write_state or record.action_in_flight:
                raise StateVersionConflict("different initial lineage transition is unresolved")
        evidence["pending_initial_lineage_transition"] = {
            "transition_key": transition_key,
            "creation_kind": "INITIAL_LOGICAL_LINEAGE",
            "current_generation": 0,
            "next_generation": 1,
            "source_repository": repository,
            "source_name": source_name,
            "starting_branch": starting_branch,
            "candidate_sha": candidate_sha,
            "task_spec_digest": task_spec_digest,
            "provider_title_marker": transition_key[:12],
            "safe_to_blind_retry": False,
        }
        record.evidence_bindings = evidence
        record.activation_mode = "SHADOW"
        try:
            saved = store.compare_and_swap_workstream(lane_id, read.version, record)
        except StateVersionConflict:
            if attempt < 2:
                continue
            raise
        if saved.status != "OK" or saved.record is None:
            raise StateUnavailable(saved.reason or "failed to persist pending initial lineage transition")
        observed = (saved.record.evidence_bindings or {}).get("pending_initial_lineage_transition")
        if not isinstance(observed, Mapping) or str(observed.get("transition_key") or "") != transition_key:
            raise StateUnavailable("pending initial lineage transition post-condition not observed")
        return {"status": "PENDING_INITIAL_LINEAGE_PERSISTED", "version": saved.version}
    raise StateUnavailable("pending initial lineage persistence exhausted CAS attempts")


def _persist_initial_binding(
    store: Any,
    *,
    lane_id: str,
    role: str,
    workstream: str,
    session_fp: str,
    source_name: str,
    repository: str,
    starting_branch: str,
    authority_event_id: str,
    operation_key: str,
    transition_key: str,
    candidate_sha: str | None,
    task_spec_digest: str,
    policy_provenance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    for attempt in range(3):
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or "initial lineage lane unavailable for binding")
        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        evidence = dict(record.evidence_bindings or {})
        generation = int(evidence.get("generation") or 0)
        existing_fp = str(evidence.get("session_fingerprint") or "").strip() or None
        existing_key = str(evidence.get("initial_lineage_transition_key") or "").strip() or None
        if generation == 1 and existing_fp:
            if existing_fp == session_fp and existing_key == transition_key:
                return {
                    "status": "IDEMPOTENT_INITIAL_BINDING_PRESENT",
                    "version": read.version,
                    "generation": 1,
                    "session_fingerprint": session_fp,
                }
            raise StateVersionConflict("initial logical lineage already has a different generation-1 binding")
        if generation != 0 or existing_fp:
            raise StateVersionConflict("initial logical lineage is no longer generation zero")

        evidence.pop("pending_initial_lineage_transition", None)
        evidence.update(
            {
                "schema_version": "1.0",
                "role": _state_role(role),
                "workstream": workstream,
                "generation": 1,
                "session_fingerprint": session_fp,
                "previous_session_fingerprint": None,
                "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                "source_name_fingerprint": sha256(source_name.encode("utf-8")).hexdigest(),
                "source_repository": repository,
                "provider_starting_branch": starting_branch,
                "initial_lineage_transition_key": transition_key,
                "generation_transition_key": transition_key,
                "generation_operation_key": operation_key,
                "task_spec_digest": task_spec_digest,
                "current_candidate_sha": candidate_sha,
                "binding_status": "PROVEN",
                "binding_reason": "AUTHORITATIVE_PROVIDER_CREATE_READBACK",
                "raw_session_id_persisted": False,
            }
        )
        record.activation_mode = "SHADOW"
        record.actor_bindings = {
            _state_role(role): {
                "provider": "jules",
                "proof_status": "PROVEN_EXPLICIT_INITIAL_LINEAGE_READBACK",
                "session_fingerprint": session_fp,
                "source_repository": repository,
                "provider_starting_branch": starting_branch,
                "raw_session_id_persisted": False,
            }
        }
        record.authority_provenance = {
            **(record.authority_provenance or {}),
            "authority_event_id": authority_event_id,
            "scope": INITIAL_SCOPE,
            "effect_scope_active": False,
            "policy_provenance": dict(policy_provenance or {}),
            "adapter_live_session_identity_is_authority": False,
        }
        record.evidence_bindings = evidence
        record.unknown_write_state = None
        record.last_observed_provider_state = {
            "binding_status": "PROVEN",
            "generation": 1,
            "session_fingerprint": session_fp,
            "provider_starting_branch": starting_branch,
            "raw_session_id_persisted": False,
        }
        record.last_successful_transition = {
            "kind": "INITIAL_LOGICAL_LINEAGE_BOUND",
            "generation": 1,
            "initial_lineage_transition_key": transition_key,
            "operation_key": operation_key,
        }
        try:
            saved = store.compare_and_swap_workstream(lane_id, read.version, record)
        except StateVersionConflict:
            if attempt < 2:
                continue
            raise
        if saved.status != "OK" or saved.record is None:
            raise StateUnavailable(saved.reason or "failed to persist initial logical lineage binding")
        observed = saved.record.evidence_bindings or {}
        if (
            int(observed.get("generation") or 0) != 1
            or str(observed.get("session_fingerprint") or "") != session_fp
            or str(observed.get("initial_lineage_transition_key") or "") != transition_key
            or observed.get("pending_initial_lineage_transition") is not None
        ):
            raise StateUnavailable("initial lineage binding post-condition not observed")
        return {
            "status": "INITIAL_LINEAGE_BINDING_PERSISTED",
            "version": saved.version,
            "generation": 1,
            "session_fingerprint": session_fp,
        }
    raise StateUnavailable("initial lineage binding persistence exhausted CAS attempts")


def execute_initial_lineage_generation(
    store: Any,
    client: Any,
    *,
    authority: Mapping[str, Any] | None,
    current_policy: Mapping[str, Any],
    project: str,
    route: str,
    workstream: str,
    role: str,
    task_spec: Mapping[str, Any],
    prompt: str,
    title: str,
    source_name: str,
    starting_branch: str,
    repository: str,
    candidate_sha: str | None,
    active_duplicate_absent: bool,
    exact_repository_binding: bool,
    exact_starting_ref_binding: bool,
) -> dict[str, Any]:
    """Create the first Jules physical generation for an explicitly authorized logical lineage.

    This primitive is not called by the scheduled runtime in A2. It performs a
    provider mutation only when the exact fresh Current Authority contains the
    matching initial-lineage entry and its task_spec is byte-equivalent under
    canonical JSON to the task_spec supplied to this call.
    """

    lane_authority = initial_lineage_authority(authority, workstream=workstream, role=role)
    event_id = str((authority or {}).get("authority_event_id") or "").strip()
    authorized_spec = lane_authority.get("task_spec") if isinstance(lane_authority, Mapping) else None
    if not event_id or not isinstance(authorized_spec, Mapping):
        return {
            "decision": "INITIAL_LINEAGE_CURRENT_AUTHORITY_REQUIRED",
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }
    supplied_spec = dict(task_spec) if isinstance(task_spec, Mapping) else {}
    if not supplied_spec or _digest(authorized_spec) != _digest(supplied_spec):
        return {
            "decision": "INITIAL_LINEAGE_TASK_SPEC_AUTHORITY_MISMATCH",
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }
    if not prompt.strip() or not title.strip() or not source_name.strip() or not starting_branch.strip() or not repository.strip():
        return {
            "decision": "INITIAL_LINEAGE_EXECUTION_SPEC_INCOMPLETE",
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }

    lane_id = _ensure_initial_lane(
        store,
        project=project,
        route=route,
        workstream=workstream,
        role=role,
        candidate_sha=candidate_sha,
    )
    before = _snapshot(store, lane_id)
    task_spec_digest = _digest(supplied_spec)

    if (
        before["generation"] == 1
        and before["session_fingerprint"]
        and before["initial_lineage_transition_key"]
    ):
        return {
            "decision": "IDEMPOTENT_INITIAL_LINEAGE_ALREADY_BOUND",
            "provider_write_attempted": False,
            "generation": 1,
            "session_fingerprint": before["session_fingerprint"],
            "safe_to_blind_retry": False,
        }

    transition = assess_initial_lineage_creation(
        project=project,
        route=route,
        workstream=workstream,
        role=role,
        current_generation=before["generation"],
        predecessor_session_fingerprint=before["session_fingerprint"],
        candidate_sha=candidate_sha,
        current_policy=current_policy,
        active_duplicate_absent=active_duplicate_absent,
        unknown_write_state=bool(before["unknown_write_state"]),
        effect_in_flight=bool(before["action_in_flight"]),
        exact_repository_binding=exact_repository_binding,
        exact_starting_ref_binding=exact_starting_ref_binding,
        initial_task_spec=supplied_spec,
    )
    if not transition["allowed"]:
        return {
            "decision": "INITIAL_LINEAGE_BLOCKED",
            "provider_write_attempted": False,
            "transition": transition,
            "safe_to_blind_retry": False,
        }

    transition_key = str(transition["transition_key"])
    pending = _persist_pending_initial(
        store,
        lane_id=lane_id,
        transition=transition,
        repository=repository,
        source_name=source_name,
        starting_branch=starting_branch,
        candidate_sha=candidate_sha,
        task_spec_digest=task_spec_digest,
    )

    _authorize_lane(
        store,
        project=project,
        route=route,
        workstream=workstream,
        role=_state_role(role),
        authority_event_id=event_id,
        scope=INITIAL_SCOPE,
    )
    try:
        source_fp = sha256(source_name.encode("utf-8")).hexdigest()
        effect = canonical_effect_identity(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=f"LINEAGE::{workstream}::{_state_role(role)}",
            action=INITIAL_ACTION,
            target={
                "provider": "jules",
                "role": _state_role(role),
                "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                "generation": "1",
                "source_fingerprint": source_fp,
                "starting_branch": starting_branch,
                "transition_key": transition_key,
            },
        )
        operation_key = effect_operation_key(effect)
        request_digest = canonical_request_digest(
            {
                "prompt": prompt,
                "title": title,
                "task_spec_digest": task_spec_digest,
                "transition_key": transition_key,
            }
        )
        authorization = MutationAuthorization(
            effect_identity=effect,
            authority_event_id=event_id,
            project_policy_authorized=True,
            exact_binding_proven=True,
            evidence_verified=True,
            expires_at=_iso(datetime.now(timezone.utc) + timedelta(minutes=10)),
        )
        claim = claim_operation(
            store,
            lane_id=lane_id,
            owner="ues-initial-lineage-lifecycle",
            operation_key=operation_key,
            action=INITIAL_ACTION,
            request_digest=request_digest,
            ttl_seconds=DEFAULT_TTL_SECONDS,
            receipt={
                "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                "generation": 1,
                "source_fingerprint": source_fp,
                "starting_branch": starting_branch,
                "task_spec_digest": task_spec_digest,
                "transition_key": transition_key,
                "raw_session_id_persisted": False,
            },
            effect_identity=effect,
            authorization=authorization,
            observed_start={"generation": "0", "session_fingerprint": "NONE"},
        )
        if claim.get("decision") != "CLAIMED" or not claim.get("mutation_allowed"):
            decision = str(claim.get("decision") or "CLAIM_DENIED")
            if decision == "IDEMPOTENT_REPLAY_CONFIRMED":
                decision = "INITIAL_LINEAGE_REPLAY_REQUIRES_BINDING_RECONCILIATION"
            return {
                "decision": decision,
                "provider_write_attempted": False,
                "operation_key": operation_key,
                "transition": transition,
                "pending_transition": pending,
                "safe_to_blind_retry": False,
            }

        try:
            receipt = client.create_session(
                prompt=prompt,
                title=f"{title} [{transition_key[:12]}]",
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
                    "category": "INITIAL_LINEAGE_CREATE_OUTCOME_UNKNOWN",
                    "recovery_verdict": recovery.get("verdict") if isinstance(recovery, Mapping) else None,
                    "initial_lineage_transition_key": transition_key,
                    "safe_to_blind_retry": False,
                },
            )
            return {
                "decision": "INITIAL_LINEAGE_CREATE_UNKNOWN_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
                "transition": transition,
                "pending_transition": pending,
                "safe_to_blind_retry": False,
            }
        except ProviderError as exc:
            record_unknown_write(
                store,
                lane_id=lane_id,
                operation_key=operation_key,
                result={
                    "category": getattr(exc, "category", type(exc).__name__),
                    "initial_lineage_transition_key": transition_key,
                    "safe_to_blind_retry": False,
                },
            )
            return {
                "decision": "INITIAL_LINEAGE_PROVIDER_ERROR_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
                "safe_to_blind_retry": False,
            }
        except Exception as exc:
            record_unknown_write(
                store,
                lane_id=lane_id,
                operation_key=operation_key,
                result={
                    "category": "INITIAL_LINEAGE_UNEXPECTED_PROVIDER_ERROR",
                    "error_type": type(exc).__name__,
                    "initial_lineage_transition_key": transition_key,
                    "safe_to_blind_retry": False,
                },
            )
            return {
                "decision": "INITIAL_LINEAGE_UNEXPECTED_PROVIDER_ERROR_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
                "safe_to_blind_retry": False,
            }

        raw_session = str(receipt.get("session") or "").strip()
        receipt_repository = str(receipt.get("repository") or "").strip()
        receipt_branch = str(receipt.get("starting_branch") or "").strip()
        if not raw_session or receipt_repository.casefold() != repository.casefold() or receipt_branch != starting_branch:
            record_unknown_write(
                store,
                lane_id=lane_id,
                operation_key=operation_key,
                result={
                    "category": "INITIAL_LINEAGE_PROVIDER_READBACK_BINDING_MISMATCH",
                    "initial_lineage_transition_key": transition_key,
                    "safe_to_blind_retry": False,
                },
            )
            return {
                "decision": "INITIAL_LINEAGE_PROVIDER_READBACK_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
                "safe_to_blind_retry": False,
            }

        new_fp = session_fingerprint(raw_session)
        record_authoritative_readback(
            store,
            lane_id=lane_id,
            operation_key=operation_key,
            observed=True,
            evidence={
                "outcome": "INITIAL_LINEAGE_SESSION_CREATED",
                "session_fingerprint": new_fp,
                "repository": repository,
                "starting_branch": starting_branch,
                "generation": 1,
                "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                "initial_lineage_transition_key": transition_key,
                "raw_session_id_persisted": False,
                "safe_to_blind_retry": False,
            },
        )
        try:
            binding = _persist_initial_binding(
                store,
                lane_id=lane_id,
                role=role,
                workstream=workstream,
                session_fp=new_fp,
                source_name=source_name,
                repository=repository,
                starting_branch=starting_branch,
                authority_event_id=event_id,
                operation_key=operation_key,
                transition_key=transition_key,
                candidate_sha=candidate_sha,
                task_spec_digest=task_spec_digest,
                policy_provenance=current_policy.get("provenance") if isinstance(current_policy.get("provenance"), Mapping) else {},
            )
            accounting = record_confirmed_generation(
                store,
                project=project,
                route=route,
                operation_key=operation_key,
                generation_transition_key=transition_key,
            )
        except Exception as exc:
            record_unknown_write(
                store,
                lane_id=lane_id,
                operation_key=operation_key,
                result={
                    "category": "INITIAL_LINEAGE_STATESTORE_HANDOFF_FAILED",
                    "error_type": type(exc).__name__,
                    "initial_lineage_transition_key": transition_key,
                    "safe_to_blind_retry": False,
                },
            )
            return {
                "decision": "INITIAL_LINEAGE_CREATED_STATESTORE_RECONCILIATION_REQUIRED",
                "provider_write_attempted": True,
                "operation_key": operation_key,
                "safe_to_blind_retry": False,
            }

        after = _snapshot(store, lane_id)
        if (
            after["generation"] != 1
            or after["session_fingerprint"] != new_fp
            or after["initial_lineage_transition_key"] != transition_key
            or after["pending"] is not None
        ):
            raise StateUnavailable("initial lineage exact post-condition mismatch")

        return {
            "decision": "INITIAL_LOGICAL_LINEAGE_GENERATION_CONFIRMED",
            "provider_write_attempted": True,
            "external_effects_dispatched": 1,
            "new_tasks_or_sessions_created": 1,
            "operation_key": operation_key,
            "transition": transition,
            "generation_binding": binding,
            "budget_accounting": accounting,
            "generation": 1,
            "session_fingerprint": new_fp,
            "safe_to_blind_retry": False,
        }
    finally:
        _restore_lane_shadow(store, lane_id, authority_event_id=event_id)
