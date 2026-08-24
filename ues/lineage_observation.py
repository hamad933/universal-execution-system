from __future__ import annotations

from typing import Any, Mapping

from .lineage_registry import lineage_lane_id, upsert_lineage_observation
from .state_store import StateUnavailable, StateVersionConflict, WorkstreamRuntimeRecord


def upsert_lineage_observation_preserving_effects(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    binding: Mapping[str, Any],
    policy: Mapping[str, Any],
    current_candidate_sha: str | None = None,
    current_pr_number: int | None = None,
) -> dict[str, Any]:
    """Passive observation cannot erase unresolved external-effect evidence."""

    lane_id = lineage_lane_id(project, route, workstream, role)
    before = store.read_workstream(lane_id)
    preserved: dict[str, Any] = {}
    unknown = None
    if before.status == "OK" and before.record is not None:
        evidence = before.record.evidence_bindings or {}
        pending = evidence.get("pending_generation_transition")
        if isinstance(pending, Mapping):
            preserved["pending_generation_transition"] = dict(pending)
        unknown = before.record.unknown_write_state

    result = upsert_lineage_observation(
        store,
        project=project,
        route=route,
        workstream=workstream,
        role=role,
        binding=binding,
        policy=policy,
        current_candidate_sha=current_candidate_sha,
        current_pr_number=current_pr_number,
    )
    if not preserved:
        return result

    for attempt in range(3):
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or "lineage state unavailable while restoring pending effect evidence")
        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        evidence = dict(record.evidence_bindings or {})
        evidence.update(preserved)
        record.evidence_bindings = evidence
        if unknown is not None:
            record.unknown_write_state = dict(unknown) if isinstance(unknown, Mapping) else unknown
        record.activation_mode = "SHADOW"
        try:
            saved = store.compare_and_swap_workstream(lane_id, read.version, record)
        except StateVersionConflict:
            if attempt < 2:
                continue
            raise
        if saved.status != "OK" or saved.record is None:
            raise StateUnavailable(saved.reason or "failed to preserve pending generation evidence")
        observed = (saved.record.evidence_bindings or {}).get("pending_generation_transition")
        expected = preserved["pending_generation_transition"]
        if not isinstance(observed, Mapping) or str(observed.get("transition_key") or "") != str(expected.get("transition_key") or ""):
            raise StateUnavailable("pending generation effect evidence post-condition not preserved")
        result = dict(result)
        result["unresolved_effect_evidence_preserved"] = True
        return result
    raise StateUnavailable("pending generation preservation exhausted CAS attempts")
