from __future__ import annotations

from hashlib import sha256
from typing import Any, Mapping

from .lineage_registry import lineage_lane_id, normalize_role
from .state_store import StateUnavailable, StateVersionConflict, WorkstreamRuntimeRecord


def _confirmed_receipt_session_fingerprint(record: WorkstreamRuntimeRecord) -> str | None:
    """Recover only an already-authoritative initial creation fingerprint."""

    receipt = record.operation_receipt if isinstance(record.operation_receipt, Mapping) else {}
    if str(receipt.get("state") or "").upper() != "CONFIRMED":
        return None
    creation_kind = str(receipt.get("creation_kind") or "").upper()
    if creation_kind and creation_kind != "INITIAL_LOGICAL_LINEAGE":
        return None
    post = receipt.get("post_condition") if isinstance(receipt.get("post_condition"), Mapping) else {}
    if post.get("observed") is not True:
        return None
    evidence = post.get("evidence") if isinstance(post.get("evidence"), Mapping) else {}
    fp = str(evidence.get("session_fingerprint") or "").strip().lower()
    if len(fp) != 64 or any(ch not in "0123456789abcdef" for ch in fp):
        return None
    return fp


def recover_lineage_policy_from_state(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    stable_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay only durable binding facts from StateStore onto stable lineage config.

    This is not project-authority resolution. It recovers the last provider
    generation identity so a fresh runner can rebind without committing a live
    session fingerprint into the project adapter.
    """

    result = dict(stable_policy)
    lane_id = lineage_lane_id(project, route, workstream, role)
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        result["_state_generation_recovered"] = False
        return result

    evidence = read.record.evidence_bindings or {}
    current_fp = str(evidence.get("session_fingerprint") or "").strip().lower()
    recovery_source = "EVIDENCE_BINDINGS" if current_fp else None
    if not current_fp:
        current_fp = _confirmed_receipt_session_fingerprint(read.record) or ""
        if current_fp:
            recovery_source = "CONFIRMED_CREATION_RECEIPT"

    known = {
        str(item).strip().lower()
        for item in result.get("known_session_fingerprints") or []
        if str(item).strip()
    }
    if current_fp:
        known.add(current_fp)
        result["known_session_fingerprints"] = sorted(known)

    persisted_branch = str(evidence.get("provider_starting_branch") or "").strip()
    if not persisted_branch and recovery_source == "CONFIRMED_CREATION_RECEIPT":
        receipt = read.record.operation_receipt if isinstance(read.record.operation_receipt, Mapping) else {}
        post = receipt.get("post_condition") if isinstance(receipt.get("post_condition"), Mapping) else {}
        post_evidence = post.get("evidence") if isinstance(post.get("evidence"), Mapping) else {}
        persisted_branch = str(post_evidence.get("starting_branch") or receipt.get("starting_branch") or "").strip()
    if persisted_branch and not str(result.get("provider_starting_branch") or "").strip():
        result["provider_starting_branch"] = persisted_branch

    result["_state_generation_recovered"] = bool(current_fp)
    result["_state_generation_recovery_source"] = recovery_source
    result["_state_generation"] = int(evidence.get("generation") or 0)
    result["_state_transition_key"] = evidence.get("generation_transition_key")
    return result


def persist_created_generation_binding(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    generation: int,
    session_fingerprint: str,
    source_name: str,
    source_repository: str,
    provider_starting_branch: str,
    authority_event_id: str,
    operation_key: str,
    generation_transition_key: str,
    replacement_cause: str,
    candidate_sha: str | None,
    policy_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Durably bind a provider-created physical generation to its logical lineage."""

    role_name = normalize_role("ASSURANCE" if str(role).upper() == "FINAL_ASSURANCE" else role)
    fp = str(session_fingerprint or "").strip().lower()
    if len(fp) != 64:
        raise ValueError("session_fingerprint must be a SHA-256 hex digest")
    if generation < 1:
        raise ValueError("generation must be positive")
    if not provider_starting_branch.strip() or not source_repository.strip() or not source_name.strip():
        raise ValueError("source repository/name and provider starting branch are required")
    if not generation_transition_key.strip() or not operation_key.strip():
        raise ValueError("generation transition and operation keys are required")

    lane_id = lineage_lane_id(project, route, workstream, role_name)
    for attempt in range(3):
        read = store.read_workstream(lane_id)
        if read.status == "MISSING":
            record = WorkstreamRuntimeRecord(
                lane_id=lane_id,
                project=project,
                route=route,
                workstream_id=f"LINEAGE::{workstream}::{role_name}",
                activation_mode="SHADOW",
            )
            expected = 0
        elif read.status == "OK" and read.record is not None:
            record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
            expected = read.version
        else:
            raise StateUnavailable(read.reason or f"lineage state unavailable: {lane_id}")

        previous = record.evidence_bindings or {}
        previous_generation = int(previous.get("generation") or 0)
        previous_fp = str(previous.get("session_fingerprint") or "").strip().lower() or None
        previous_transition = str(previous.get("generation_transition_key") or "").strip() or None

        if previous_generation > generation:
            raise StateVersionConflict("persisted lineage generation is newer than created generation")
        if previous_generation == generation and previous_fp:
            if previous_fp != fp or (previous_transition and previous_transition != generation_transition_key):
                raise StateVersionConflict("generation identity conflicts with durable StateStore binding")
            return {
                "status": "IDEMPOTENT_BINDING_PRESENT",
                "lane_id": lane_id,
                "version": read.version,
                "generation": generation,
                "session_fingerprint": fp,
            }
        if previous_generation and generation != previous_generation + 1:
            raise StateVersionConflict("generation transition is not contiguous")

        record.activation_mode = "SHADOW"
        record.actor_bindings = {
            role_name: {
                "provider": "jules",
                "proof_status": "PROVEN_EXPLICIT_GENERATION_READBACK",
                "session_fingerprint": fp,
                "source_repository": source_repository,
                "provider_starting_branch": provider_starting_branch,
                "raw_session_id_persisted": False,
            }
        }
        record.authority_provenance = {
            **(record.authority_provenance or {}),
            "authority_event_id": authority_event_id,
            "scope": "NEXT_PHYSICAL_GENERATION_SAME_LOGICAL_LINEAGE",
            "effect_scope_active": False,
            "policy_provenance": dict(policy_provenance or {}),
            "adapter_live_session_identity_is_authority": False,
        }
        record.evidence_bindings = {
            **previous,
            "schema_version": "1.2",
            "role": role_name,
            "workstream": workstream,
            "generation": generation,
            "session_fingerprint": fp,
            "previous_session_fingerprint": previous_fp,
            "source_name_fingerprint": sha256(source_name.encode("utf-8")).hexdigest(),
            "source_repository": source_repository,
            "provider_starting_branch": provider_starting_branch,
            "generation_transition_key": generation_transition_key,
            "generation_operation_key": operation_key,
            "replacement_cause": replacement_cause,
            "current_candidate_sha": candidate_sha,
            "binding_status": "PROVEN",
            "binding_reason": "AUTHORITATIVE_PROVIDER_CREATE_READBACK",
            "raw_session_id_persisted": False,
        }
        record.unknown_write_state = None
        record.last_observed_provider_state = {
            "binding_status": "PROVEN",
            "generation": generation,
            "session_fingerprint": fp,
            "provider_starting_branch": provider_starting_branch,
            "raw_session_id_persisted": False,
        }
        record.last_successful_transition = {
            "kind": "PHYSICAL_GENERATION_BOUND",
            "generation": generation,
            "generation_transition_key": generation_transition_key,
            "operation_key": operation_key,
        }
        try:
            saved = store.compare_and_swap_workstream(lane_id, expected, record)
        except StateVersionConflict:
            if attempt < 2:
                continue
            raise
        if saved.status != "OK" or saved.record is None:
            raise StateUnavailable(saved.reason or "failed to persist created generation binding")
        observed = saved.record.evidence_bindings or {}
        if (
            int(observed.get("generation") or 0) != generation
            or str(observed.get("session_fingerprint") or "").lower() != fp
            or str(observed.get("generation_transition_key") or "") != generation_transition_key
        ):
            raise StateUnavailable("generation binding post-condition was not observed")
        return {
            "status": "GENERATION_BINDING_PERSISTED",
            "lane_id": lane_id,
            "version": saved.version,
            "generation": generation,
            "session_fingerprint": fp,
        }
    raise StateUnavailable("generation binding persistence exhausted CAS attempts")
