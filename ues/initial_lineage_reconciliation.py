from __future__ import annotations

from typing import Any, Mapping, Sequence

from .initial_lineage_effects import _persist_initial_binding
from .lineage_registry import lineage_lane_id, session_fingerprint
from .state_store import StateUnavailable, record_authoritative_readback, record_unknown_write
from .task_budget_accounting import record_confirmed_generation


def _pending_initial_state(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
) -> tuple[str, Mapping[str, Any], str, str, Mapping[str, Any]]:
    state_role = "ASSURANCE" if str(role).upper() == "FINAL_ASSURANCE" else str(role).upper()
    lane_id = lineage_lane_id(project, route, workstream, state_role)
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable(read.reason or "initial lineage state unavailable for reconciliation")
    record = read.record
    unknown = record.unknown_write_state
    if not isinstance(unknown, Mapping):
        raise StateUnavailable("initial lineage reconciliation requires durable UNKNOWN write state")
    operation_key = str(unknown.get("operation_key") or "").strip()
    evidence = record.evidence_bindings or {}
    pending = evidence.get("pending_initial_lineage_transition")
    if not operation_key or not isinstance(pending, Mapping):
        raise StateUnavailable("UNKNOWN initial lineage state lacks operation/pending transition evidence")
    if str(pending.get("creation_kind") or "").strip().upper() != "INITIAL_LOGICAL_LINEAGE":
        raise StateUnavailable("pending transition is not an initial logical lineage create")
    if int(pending.get("current_generation") or -1) != 0 or int(pending.get("next_generation") or 0) != 1:
        raise StateUnavailable("pending initial lineage transition has invalid generation boundary")
    provenance = record.authority_provenance or {}
    authority_event_id = str(
        provenance.get("last_effect_authority_event_id")
        or provenance.get("authority_event_id")
        or ""
    ).strip()
    if not authority_event_id:
        raise StateUnavailable("UNKNOWN initial lineage state lacks durable authority provenance")
    policy_provenance = provenance.get("policy_provenance")
    policy_provenance = dict(policy_provenance) if isinstance(policy_provenance, Mapping) else {}
    return lane_id, pending, operation_key, authority_event_id, policy_provenance


def _matching_sessions(
    inventory: Sequence[Mapping[str, Any]],
    *,
    repository: str,
    source_name: str,
    starting_branch: str,
    provider_title_marker: str,
) -> list[Mapping[str, Any]]:
    marker = f"[{provider_title_marker}]"
    expected_source = source_name.strip().strip("/")
    matches: list[Mapping[str, Any]] = []
    for session in inventory:
        if str(session.get("_source_repository") or "").casefold() != repository.casefold():
            continue
        if str(session.get("_source_name") or "").strip().strip("/") != expected_source:
            continue
        if str(session.get("sourceStartingBranch") or "") != starting_branch:
            continue
        title = str(session.get("title") or session.get("displayName") or "")
        if marker not in title:
            continue
        name = str(session.get("name") or "").strip()
        if not name:
            continue
        matches.append(session)
    return matches


def reconcile_unknown_initial_lineage(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    inventory: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Adopt one UNKNOWN initial Jules create only from authoritative enumeration.

    This function performs no provider mutation. Zero matches remain UNKNOWN to
    tolerate eventual consistency. Multiple matches remain UNKNOWN as a possible
    duplicate. Exactly one repository/source/ref/title-marker match is adopted
    into generation 1 and accounted without issuing another createSession.
    """

    lane_id, pending, operation_key, authority_event_id, policy_provenance = _pending_initial_state(
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
        source_name=source_name,
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
    raw_name = str(session.get("name") or "").strip()
    fp = session_fingerprint(raw_name)
    evidence = {
        "outcome": "INITIAL_LINEAGE_SESSION_CREATED_RECONCILED",
        "session_fingerprint": fp,
        "repository": repository,
        "source_name_fingerprint_only": True,
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
            session_fp=fp,
            source_name=source_name,
            repository=repository,
            starting_branch=starting_branch,
            authority_event_id=authority_event_id,
            operation_key=operation_key,
            transition_key=transition_key,
            candidate_sha=candidate_sha,
            task_spec_digest=task_spec_digest,
            policy_provenance=policy_provenance,
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

    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable("reconciled initial lineage lane unavailable after binding")
    post = read.record.evidence_bindings or {}
    if (
        int(post.get("generation") or 0) != 1
        or str(post.get("session_fingerprint") or "") != fp
        or str(post.get("initial_lineage_transition_key") or "") != transition_key
        or str(post.get("task_spec_digest") or "") != task_spec_digest
        or post.get("pending_initial_lineage_transition") is not None
        or read.record.unknown_write_state is not None
        or read.record.activation_mode != "SHADOW"
    ):
        raise StateUnavailable("reconciled initial lineage post-condition mismatch")

    return {
        "decision": "UNKNOWN_INITIAL_LINEAGE_AUTHORITATIVELY_RECONCILED",
        "provider_write_attempted": False,
        "match_count": 1,
        "operation_key": operation_key,
        "transition_key": transition_key,
        "generation": 1,
        "session_fingerprint": fp,
        "generation_binding": binding,
        "budget_accounting": accounting,
        "safe_to_blind_retry": False,
    }
