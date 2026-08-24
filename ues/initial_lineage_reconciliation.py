from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping, Sequence

from .initial_lineage_effects import _persist_initial_binding
from .lineage_registry import lineage_lane_id, session_fingerprint
from .state_store import (
    StateUnavailable,
    StateVersionConflict,
    WorkstreamRuntimeRecord,
    record_authoritative_readback,
    record_unknown_write,
)
from .task_budget_accounting import record_confirmed_generation


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _state_role(role: str) -> str:
    return "ASSURANCE" if str(role).upper() == "FINAL_ASSURANCE" else str(role).upper()


def _pending_initial_state(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
) -> tuple[str, Mapping[str, Any], str]:
    lane_id = lineage_lane_id(project, route, workstream, _state_role(role))
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable(read.reason or "initial lineage state unavailable for reconciliation")
    unknown = read.record.unknown_write_state
    if not isinstance(unknown, Mapping):
        raise StateUnavailable("initial lineage reconciliation requires durable UNKNOWN write state")
    operation_key = str(unknown.get("operation_key") or "").strip()
    pending = (read.record.evidence_bindings or {}).get("pending_initial_lineage_transition")
    if not operation_key or not isinstance(pending, Mapping):
        raise StateUnavailable("UNKNOWN initial lineage state is missing operation/pending transition evidence")
    if str(pending.get("creation_kind") or "").upper() != "INITIAL_LOGICAL_LINEAGE":
        raise StateUnavailable("pending initial lineage state has an unexpected creation kind")
    return lane_id, pending, operation_key


def _matching_sessions(
    inventory: Sequence[Mapping[str, Any]],
    *,
    repository: str,
    starting_branch: str,
    provider_title_marker: str,
) -> list[Mapping[str, Any]]:
    marker = f"[{provider_title_marker}]"
    result: list[Mapping[str, Any]] = []
    for session in inventory:
        if str(session.get("_source_repository") or "").casefold() != repository.casefold():
            continue
        if str(session.get("sourceStartingBranch") or "") != starting_branch:
            continue
        title = str(session.get("title") or session.get("displayName") or "")
        if marker not in title:
            continue
        if not str(session.get("name") or "").strip():
            continue
        result.append(session)
    return result


def reconcile_exact_initial_lineage_marker(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    inventory: Sequence[Mapping[str, Any]],
    authority_event_id: str,
    repository: str,
    source_name: str,
    starting_branch: str,
    provider_title_marker: str,
    transition_key: str,
    candidate_sha: str | None,
    task_spec_digest: str,
    policy_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind a generation-zero lineage from one exact deterministic provider marker.

    This is a read-only reconciliation path. It never creates, cancels, or updates a
    provider session. Repository + provider branch + the task-derived UES transition
    marker must identify exactly one provider session. The existing lane must still
    be generation zero and free of UNKNOWN/in-flight effects.
    """

    event_id = str(authority_event_id or "").strip()
    repository = str(repository or "").strip()
    source_name = str(source_name or "").strip()
    starting_branch = str(starting_branch or "").strip()
    marker = str(provider_title_marker or "").strip()
    transition_key = str(transition_key or "").strip()
    task_spec_digest = str(task_spec_digest or "").strip()
    if not all((event_id, repository, source_name, starting_branch, marker, transition_key, task_spec_digest)):
        return {
            "decision": "INITIAL_LINEAGE_MARKER_RECONCILIATION_INPUT_REQUIRED",
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }

    lane_id = lineage_lane_id(project, route, workstream, _state_role(role))
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable(read.reason or "initial lineage state unavailable for marker reconciliation")
    prior = read.record.evidence_bindings or {}
    if (
        int(prior.get("generation") or 0) != 0
        or str(prior.get("session_fingerprint") or "").strip()
        or read.record.unknown_write_state is not None
        or read.record.action_in_flight is not None
    ):
        return {
            "decision": "INITIAL_LINEAGE_MARKER_RECONCILIATION_REQUIRES_CLEAN_GENERATION_ZERO",
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }

    matches = _matching_sessions(
        inventory,
        repository=repository,
        starting_branch=starting_branch,
        provider_title_marker=marker,
    )
    if not matches:
        return {
            "decision": "INITIAL_LINEAGE_EXACT_MARKER_NOT_FOUND",
            "provider_write_attempted": False,
            "match_count": 0,
            "transition_key": transition_key,
            "safe_to_blind_retry": False,
        }
    if len(matches) != 1:
        return {
            "decision": "INITIAL_LINEAGE_EXACT_MARKER_AMBIGUOUS",
            "provider_write_attempted": False,
            "match_count": len(matches),
            "transition_key": transition_key,
            "safe_to_blind_retry": False,
        }

    session = matches[0]
    session_name = str(session.get("name") or "").strip()
    session_fp = session_fingerprint(session_name)
    provider_state = str(session.get("normalizedState") or session.get("state") or "UNKNOWN").upper()
    operation_key = sha256(
        f"initial-marker-reconciliation|{project}|{route}|{workstream}|{_state_role(role)}|{transition_key}|{session_fp}".encode(
            "utf-8"
        )
    ).hexdigest()

    for attempt in range(3):
        current = store.read_workstream(lane_id)
        if current.status != "OK" or current.record is None:
            raise StateUnavailable(current.reason or "initial lineage state unavailable during marker reconciliation")
        evidence = current.record.evidence_bindings or {}
        if (
            int(evidence.get("generation") or 0) != 0
            or str(evidence.get("session_fingerprint") or "").strip()
            or current.record.unknown_write_state is not None
            or current.record.action_in_flight is not None
        ):
            return {
                "decision": "INITIAL_LINEAGE_MARKER_RECONCILIATION_STATE_MOVED",
                "provider_write_attempted": False,
                "safe_to_blind_retry": False,
            }

        record = WorkstreamRuntimeRecord.from_dict(current.record.to_dict())
        record.activation_mode = "SHADOW"
        record.actor_bindings = {
            _state_role(role): {
                "provider": "jules",
                "proof_status": "PROVEN_EXACT_INITIAL_LINEAGE_MARKER",
                "session_fingerprint": session_fp,
                "source_repository": repository,
                "provider_starting_branch": starting_branch,
                "raw_session_id_persisted": False,
            }
        }
        record.authority_provenance = {
            **(record.authority_provenance or {}),
            "authority_event_id": event_id,
            "scope": "INITIAL_LOGICAL_LINEAGE_MARKER_RECONCILIATION",
            "effect_scope_active": False,
            "provider_mutation_authorized": False,
            "marker_is_task_derived_identity": True,
            "policy_provenance": dict(policy_provenance or {}),
        }
        record.evidence_bindings = {
            **dict(evidence),
            "schema_version": "1.1",
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
            "binding_reason": "AUTHORITATIVE_PROVIDER_TITLE_MARKER_READBACK",
            "provider_title_marker": marker,
            "marker_reconciliation": True,
            "raw_session_id_persisted": False,
        }
        record.unknown_write_state = None
        record.action_in_flight = None
        record.last_observed_provider_state = {
            "binding_status": "PROVEN",
            "generation": 1,
            "state": provider_state,
            "session_fingerprint": session_fp,
            "provider_starting_branch": starting_branch,
            "observed_at": _iso_now(),
            "raw_session_id_persisted": False,
        }
        record.last_successful_transition = {
            "kind": "INITIAL_LOGICAL_LINEAGE_MARKER_RECONCILED",
            "generation": 1,
            "initial_lineage_transition_key": transition_key,
            "operation_key": operation_key,
            "at": _iso_now(),
        }
        try:
            saved = store.compare_and_swap_workstream(lane_id, current.version, record)
        except StateVersionConflict:
            if attempt < 2:
                continue
            raise
        if saved.status != "OK" or saved.record is None:
            raise StateUnavailable(saved.reason or "failed to persist exact marker reconciliation")
        observed = saved.record.evidence_bindings or {}
        if (
            int(observed.get("generation") or 0) != 1
            or str(observed.get("session_fingerprint") or "") != session_fp
            or str(observed.get("initial_lineage_transition_key") or "") != transition_key
            or str(observed.get("task_spec_digest") or "") != task_spec_digest
        ):
            raise StateUnavailable("exact marker reconciliation post-condition not observed")
        return {
            "decision": "EXISTING_INITIAL_LINEAGE_EXACT_MARKER_RECONCILED",
            "provider_write_attempted": False,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "match_count": 1,
            "generation": 1,
            "session_fingerprint": session_fp,
            "transition_key": transition_key,
            "operation_key": operation_key,
            "provider_state": provider_state,
            "safe_to_blind_retry": False,
        }
    raise StateUnavailable("exact marker reconciliation exhausted CAS attempts")


def reconcile_unknown_initial_lineage(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    inventory: Sequence[Mapping[str, Any]],
    authority_event_id: str,
    policy_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Adopt an ambiguous first-generation Jules session only from exact readback.

    The reconciliation path never calls a provider mutation. Zero matches remain
    UNKNOWN because enumeration may be eventually consistent. Multiple matches
    remain ambiguous. Exactly one repository/ref/title-marker match is bound to
    generation 1 and accounted once without issuing a second createSession.
    """

    event_id = str(authority_event_id or "").strip()
    if not event_id:
        return {
            "decision": "INITIAL_LINEAGE_RECONCILIATION_CURRENT_AUTHORITY_REQUIRED",
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }

    lane_id, pending, operation_key = _pending_initial_state(
        store,
        project=project,
        route=route,
        workstream=workstream,
        role=role,
    )
    repository = str(pending.get("source_repository") or "").strip()
    source_name = str(pending.get("source_name") or "").strip()
    starting_branch = str(pending.get("starting_branch") or "").strip()
    marker = str(pending.get("provider_title_marker") or "").strip()
    transition_key = str(pending.get("transition_key") or "").strip()
    candidate_sha = str(pending.get("candidate_sha") or "").strip() or None
    task_spec_digest = str(pending.get("task_spec_digest") or "").strip()
    if not all((repository, source_name, starting_branch, marker, transition_key, task_spec_digest)):
        raise StateUnavailable("pending initial lineage transition is incomplete")

    matches = _matching_sessions(
        inventory,
        repository=repository,
        starting_branch=starting_branch,
        provider_title_marker=marker,
    )
    if not matches:
        return {
            "decision": "INITIAL_LINEAGE_UNKNOWN_NOT_YET_OBSERVED",
            "provider_write_attempted": False,
            "match_count": 0,
            "operation_key": operation_key,
            "transition_key": transition_key,
            "safe_to_blind_retry": False,
        }
    if len(matches) != 1:
        return {
            "decision": "INITIAL_LINEAGE_RECONCILIATION_AMBIGUOUS_DUPLICATE",
            "provider_write_attempted": False,
            "match_count": len(matches),
            "operation_key": operation_key,
            "transition_key": transition_key,
            "safe_to_blind_retry": False,
        }

    session = matches[0]
    session_name = str(session.get("name") or "").strip()
    session_fp = session_fingerprint(session_name)
    evidence = {
        "outcome": "INITIAL_LINEAGE_SESSION_CREATED_RECONCILED",
        "session_fingerprint": session_fp,
        "repository": repository,
        "starting_branch": starting_branch,
        "generation": 1,
        "creation_kind": "INITIAL_LOGICAL_LINEAGE",
        "initial_lineage_transition_key": transition_key,
        "provider_title_marker": marker,
        "authoritative_provider_enumeration": True,
        "raw_session_id_persisted": False,
        "safe_to_blind_retry": False,
    }
    record_authoritative_readback(
        store,
        lane_id=lane_id,
        operation_key=operation_key,
        observed=True,
        evidence=evidence,
    )
    try:
        binding = _persist_initial_binding(
            store,
            lane_id=lane_id,
            role=role,
            workstream=workstream,
            session_fp=session_fp,
            source_name=source_name,
            repository=repository,
            starting_branch=starting_branch,
            authority_event_id=event_id,
            operation_key=operation_key,
            transition_key=transition_key,
            candidate_sha=candidate_sha,
            task_spec_digest=task_spec_digest,
            policy_provenance=policy_provenance or {},
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
                "category": "RECONCILED_INITIAL_LINEAGE_STATESTORE_HANDOFF_FAILED",
                "error_type": type(exc).__name__,
                "initial_lineage_transition_key": transition_key,
                "safe_to_blind_retry": False,
            },
        )
        return {
            "decision": "INITIAL_LINEAGE_RECONCILED_STATESTORE_BINDING_REQUIRED",
            "provider_write_attempted": False,
            "match_count": 1,
            "operation_key": operation_key,
            "transition_key": transition_key,
            "safe_to_blind_retry": False,
        }

    return {
        "decision": "AMBIGUOUS_INITIAL_LINEAGE_AUTHORITATIVELY_RECONCILED",
        "provider_write_attempted": False,
        "match_count": 1,
        "operation_key": operation_key,
        "transition_key": transition_key,
        "generation": 1,
        "session_fingerprint": session_fp,
        "generation_binding": binding,
        "budget_accounting": accounting,
        "safe_to_blind_retry": False,
    }
