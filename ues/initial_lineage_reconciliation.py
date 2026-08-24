from __future__ import annotations

from typing import Any, Mapping, Sequence

from .initial_lineage_effects import _persist_initial_binding
from .lineage_registry import lineage_lane_id, session_fingerprint
from .state_store import StateUnavailable, record_authoritative_readback, record_unknown_write
from .task_budget_accounting import record_confirmed_generation


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
