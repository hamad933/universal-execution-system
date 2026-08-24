from __future__ import annotations

from typing import Any, Mapping, Sequence

from .generation_pending import clear_pending_generation_transition
from .lineage_generation import persist_created_generation_binding
from .lineage_registry import lineage_lane_id, session_fingerprint
from .state_store import StateUnavailable, record_authoritative_readback, record_unknown_write
from .task_budget_accounting import record_confirmed_generation


def _pending_generation_state(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
) -> tuple[str, Mapping[str, Any], str]:
    state_role = "ASSURANCE" if str(role).upper() == "FINAL_ASSURANCE" else str(role).upper()
    lane_id = lineage_lane_id(project, route, workstream, state_role)
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable(read.reason or "lineage state unavailable for generation reconciliation")
    runtime = read.record
    unknown = runtime.unknown_write_state
    if not isinstance(unknown, Mapping):
        raise StateUnavailable("generation reconciliation requires durable UNKNOWN write state")
    operation_key = str(unknown.get("operation_key") or "").strip()
    pending = (runtime.evidence_bindings or {}).get("pending_generation_transition")
    if not operation_key or not isinstance(pending, Mapping):
        raise StateUnavailable("UNKNOWN generation state is missing operation/pending transition evidence")
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
        name = str(session.get("name") or "").strip()
        if not name:
            continue
        result.append(session)
    return result


def reconcile_unknown_generation(
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
    """Resolve an ambiguous create only from authoritative provider enumeration.

    Zero matches remain UNKNOWN because provider enumeration may be eventually
    consistent. Multiple matches remain UNKNOWN and are classified as a
    duplicate/collision. Exactly one marker/repository/branch match is adopted,
    accounted once, and bound to the same logical lineage without another create.
    """

    lane_id, pending, operation_key = _pending_generation_state(
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
    generation = int(pending.get("next_generation") or 0)
    replacement_cause = str(pending.get("replacement_cause") or "").strip()
    candidate_sha = str(pending.get("candidate_sha") or "").strip() or None
    if not all((repository, source_name, starting_branch, marker, transition_key)) or generation < 1:
        raise StateUnavailable("pending generation transition is incomplete")

    matches = _matching_sessions(
        inventory,
        repository=repository,
        starting_branch=starting_branch,
        provider_title_marker=marker,
    )
    if not matches:
        return {
            "decision": "GENERATION_UNKNOWN_NOT_YET_OBSERVED",
            "provider_write_attempted": False,
            "match_count": 0,
            "operation_key": operation_key,
            "transition_key": transition_key,
            "safe_to_blind_retry": False,
        }
    if len(matches) != 1:
        return {
            "decision": "GENERATION_RECONCILIATION_AMBIGUOUS_DUPLICATE",
            "provider_write_attempted": False,
            "match_count": len(matches),
            "operation_key": operation_key,
            "transition_key": transition_key,
            "safe_to_blind_retry": False,
        }

    session = matches[0]
    name = str(session.get("name") or "")
    fp = session_fingerprint(name)
    evidence = {
        "outcome": "SESSION_CREATED_RECONCILED",
        "session_fingerprint": fp,
        "repository": repository,
        "starting_branch": starting_branch,
        "generation": generation,
        "generation_transition_key": transition_key,
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
        binding = persist_created_generation_binding(
            store,
            project=project,
            route=route,
            workstream=workstream,
            role=role,
            generation=generation,
            session_fingerprint=fp,
            source_name=source_name,
            source_repository=repository,
            provider_starting_branch=starting_branch,
            authority_event_id=authority_event_id,
            operation_key=operation_key,
            generation_transition_key=transition_key,
            replacement_cause=replacement_cause,
            candidate_sha=candidate_sha,
            policy_provenance=policy_provenance or {},
        )
        accounting = record_confirmed_generation(
            store,
            project=project,
            route=route,
            operation_key=operation_key,
            generation_transition_key=transition_key,
        )
        clear_pending_generation_transition(
            store,
            project=project,
            route=route,
            workstream=workstream,
            role=role,
            expected_transition_key=transition_key,
        )
    except Exception as exc:
        record_unknown_write(
            store,
            lane_id=lane_id,
            operation_key=operation_key,
            result={
                "category": "RECONCILED_PROVIDER_SESSION_STATESTORE_HANDOFF_FAILED",
                "error_type": type(exc).__name__,
                "generation_transition_key": transition_key,
                "safe_to_blind_retry": False,
            },
        )
        return {
            "decision": "GENERATION_RECONCILED_STATESTORE_BINDING_REQUIRED",
            "provider_write_attempted": False,
            "match_count": 1,
            "operation_key": operation_key,
            "transition_key": transition_key,
            "safe_to_blind_retry": False,
        }

    return {
        "decision": "AMBIGUOUS_GENERATION_AUTHORITATIVELY_RECONCILED",
        "provider_write_attempted": False,
        "match_count": 1,
        "operation_key": operation_key,
        "transition_key": transition_key,
        "generation": generation,
        "session_fingerprint": fp,
        "generation_binding": binding,
        "budget_accounting": accounting,
        "safe_to_blind_retry": False,
    }
