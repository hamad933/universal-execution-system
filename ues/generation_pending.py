from __future__ import annotations

from typing import Any, Mapping

from .lineage_registry import lineage_lane_id
from .state_store import StateUnavailable, StateVersionConflict, WorkstreamRuntimeRecord


def persist_pending_generation_transition(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    transition: Mapping[str, Any],
    source_repository: str,
    source_name: str,
    starting_branch: str,
    candidate_sha: str | None,
    replacement_cause: str,
) -> dict[str, Any]:
    state_role = "ASSURANCE" if str(role).upper() == "FINAL_ASSURANCE" else str(role).upper()
    lane_id = lineage_lane_id(project, route, workstream, state_role)
    transition_key = str(transition.get("transition_key") or "")
    next_generation = int(transition.get("next_generation") or 0)
    if not transition_key or next_generation < 1:
        raise ValueError("valid generation transition is required")

    for attempt in range(3):
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or "lineage lane missing before provider generation effect")
        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        evidence = dict(record.evidence_bindings or {})
        existing = evidence.get("pending_generation_transition")
        if isinstance(existing, Mapping):
            existing_key = str(existing.get("transition_key") or "")
            if existing_key == transition_key:
                return {
                    "status": "IDEMPOTENT_PENDING_TRANSITION_PRESENT",
                    "lane_id": lane_id,
                    "transition_key": transition_key,
                    "version": read.version,
                }
            if record.unknown_write_state or record.action_in_flight:
                raise StateVersionConflict("different generation transition is unresolved")

        evidence["pending_generation_transition"] = {
            "transition_key": transition_key,
            "next_generation": next_generation,
            "current_generation": int(transition.get("current_generation") or 0),
            "source_repository": source_repository,
            "source_name": source_name,
            "starting_branch": starting_branch,
            "candidate_sha": candidate_sha,
            "replacement_cause": replacement_cause,
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
            raise StateUnavailable(saved.reason or "failed to persist pending generation transition")
        observed = (saved.record.evidence_bindings or {}).get("pending_generation_transition")
        if not isinstance(observed, Mapping) or str(observed.get("transition_key") or "") != transition_key:
            raise StateUnavailable("pending generation transition post-condition not observed")
        return {
            "status": "PENDING_TRANSITION_PERSISTED",
            "lane_id": lane_id,
            "transition_key": transition_key,
            "version": saved.version,
        }
    raise StateUnavailable("pending generation transition persistence exhausted CAS attempts")


def clear_pending_generation_transition(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    expected_transition_key: str,
) -> None:
    state_role = "ASSURANCE" if str(role).upper() == "FINAL_ASSURANCE" else str(role).upper()
    lane_id = lineage_lane_id(project, route, workstream, state_role)
    for attempt in range(3):
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or "lineage lane unavailable while clearing pending transition")
        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        evidence = dict(record.evidence_bindings or {})
        pending = evidence.get("pending_generation_transition")
        if not isinstance(pending, Mapping):
            return
        if str(pending.get("transition_key") or "") != expected_transition_key:
            raise StateVersionConflict("cannot clear a different pending generation transition")
        evidence.pop("pending_generation_transition", None)
        record.evidence_bindings = evidence
        try:
            saved = store.compare_and_swap_workstream(lane_id, read.version, record)
        except StateVersionConflict:
            if attempt < 2:
                continue
            raise
        if saved.status == "OK":
            return
    raise StateUnavailable("pending generation transition clearing exhausted CAS attempts")
