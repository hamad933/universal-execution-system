from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

from .identity import canonical_lane_id
from .live_runtime import build_live_state_store
from .provider_observer import OBSERVATION_WORKSTREAM
from .terminal_recovery import (
    TERMINAL_RESULT_KEY,
    load_governed_projects,
    read_persisted_terminal_results,
)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: Any, *, now: datetime) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def _lane_state(
    result_state: str,
    *,
    has_exact_identity: bool,
) -> tuple[str, bool, bool, bool, str]:
    state = str(result_state or "UNKNOWN")
    if state == "PARENT_CONSUMABLE":
        return "TERMINAL_RESULT_PARENT_CONSUMABLE", False, False, True, "LANE"
    if state in {
        "COMPLETED_OUTPUT_UNCONSUMED",
        "COMPLETED_OUTPUT_UNSTRUCTURED",
        "COMPLETED_OUTPUT_UNSTRUCTURED_REQUIRES_PARENT_CONSUMPTION",
        "MALFORMED_STRUCTURED_HANDOFF",
    }:
        return state, True, True, True, "LANE"
    if state in {"RESULT_IDENTITY_UNRESOLVED", "STRUCTURED_HANDOFF_UNBOUND"}:
        return state, True, False, True, "LANE"
    if state in {"REVIEWED_SHA_MISMATCH", "RESULT_STALE_AFTER_CANDIDATE_MOVEMENT"}:
        return state, False, False, True, "LANE"
    return state, bool(has_exact_identity), bool(has_exact_identity), True, "LANE"


def run(*, now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    projects = load_governed_projects()
    try:
        store = build_live_state_store()
        lane_ids = store.discover_lane_ids()
    except Exception as exc:
        return {
            "schema_version": "1.0",
            "result": "TERMINAL_LIVENESS_WATCHDOG_STATESTORE_UNAVAILABLE",
            "cycle_status": "CONTROL_CYCLE_FAILED",
            "error_category": str(getattr(exc, "category", None) or type(exc).__name__).upper()[:120],
            "lanes": [],
            "blocked_lane_freezes_independent_lanes": False,
            "mutation_performed": False,
        }

    configured = {(item["project"], item["route"]): item for item in projects}
    lineages: dict[tuple[str, str], list[dict[str, Any]]] = {}
    exact_fps: dict[tuple[str, str], dict[str, str]] = {}
    entries: list[dict[str, Any]] = []

    for lane_id in lane_ids:
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            continue
        record = read.record
        key = (record.project, record.route)
        project = configured.get(key)
        if project is None:
            continue
        evidence = record.evidence_bindings or {}
        role = str(evidence.get("role") or "").strip().upper()
        workstream = str(evidence.get("workstream") or "").strip()
        generation = int(evidence.get("generation") or 0)
        fp = str(evidence.get("session_fingerprint") or "").strip()
        if role and workstream and generation > 0:
            lineages.setdefault(key, []).append({
                "lane_id": lane_id,
                "version": read.version,
                "role": role,
                "workstream": workstream,
                "generation": generation,
                "session_fingerprint": fp or None,
                "result": evidence.get(TERMINAL_RESULT_KEY),
                "provider_state": dict(record.last_observed_provider_state or {}),
            })
            if fp:
                exact_fps.setdefault(key, {})[fp] = lane_id

    for project in projects:
        key = (project["project"], project["route"])
        durable_results = read_persisted_terminal_results(
            store,
            project=project["project"],
            route=project["route"],
            repository=project["repository"],
        )
        by_lane = {str(item.get("lane_id") or ""): item for item in durable_results}
        for lineage in lineages.get(key, []):
            lane_id = lineage["lane_id"]
            current = by_lane.get(lane_id)
            provider_state = lineage.get("provider_state") or {}
            provider_terminal = str(provider_state.get("state") or "").upper() == "COMPLETED"
            if current is None:
                if not provider_terminal:
                    continue
                safe_recovery = bool(lineage.get("session_fingerprint"))
                entries.append({
                    "project": project["project"],
                    "route": project["route"],
                    "lane_id": lane_id,
                    "logical_workstream": lineage["workstream"],
                    "role": lineage["role"],
                    "generation": lineage["generation"],
                    "session_fingerprint": lineage.get("session_fingerprint"),
                    "state": "TERMINAL_SESSION_RESULT_NOT_PERSISTED",
                    "age_seconds": None,
                    "exact_cause": "COMPLETED_PROVIDER_STATE_WITHOUT_DURABLE_TERMINAL_RESULT",
                    "safe_recovery_available": safe_recovery,
                    "parent_action_required": False,
                    "ues_automatic_recovery_possible": safe_recovery,
                    "blocking_scope": "LANE",
                })
                continue

            state, safe_recovery, auto_recovery, parent_action, scope = _lane_state(
                str(current.get("result_state") or "UNKNOWN"),
                has_exact_identity=bool(lineage.get("session_fingerprint")),
            )
            entries.append({
                "project": project["project"],
                "route": project["route"],
                "lane_id": lane_id,
                "logical_workstream": lineage["workstream"],
                "role": lineage["role"],
                "generation": lineage["generation"],
                "session_fingerprint": lineage.get("session_fingerprint"),
                "state": state,
                "age_seconds": _age_seconds(current.get("persisted_at"), now=now),
                "exact_cause": str(current.get("result_state") or "UNKNOWN"),
                "safe_recovery_available": safe_recovery,
                "parent_action_required": parent_action,
                "ues_automatic_recovery_possible": auto_recovery,
                "blocking_scope": scope,
            })

        observation_lane = canonical_lane_id(project["project"], project["route"], OBSERVATION_WORKSTREAM)
        observation = store.read_workstream(observation_lane)
        if observation.status == "OK" and observation.record is not None:
            state = observation.record.last_observed_provider_state
            if isinstance(state, Mapping):
                observed_at = state.get("observed_at")
                raw_sessions = state.get("sessions")
                if isinstance(raw_sessions, list):
                    known_result_fps = {
                        str(item.get("session_fingerprint") or "")
                        for item in durable_results
                        if str(item.get("session_fingerprint") or "")
                    }
                    for session in raw_sessions:
                        if not isinstance(session, Mapping):
                            continue
                        if str(session.get("state") or "").upper() != "COMPLETED":
                            continue
                        fp = str(session.get("session_fingerprint") or "").strip()
                        if fp and fp in known_result_fps:
                            continue
                        lane_id = exact_fps.get(key, {}).get(fp)
                        if lane_id:
                            cause = "PROVIDER_READ_COMPLETE_BUT_DURABLE_RESULT_MISSING"
                            recovery = True
                            identity = True
                        else:
                            cause = "RESULT_IDENTITY_UNRESOLVED"
                            recovery = False
                            identity = False
                        entries.append({
                            "project": project["project"],
                            "route": project["route"],
                            "lane_id": lane_id,
                            "logical_workstream": None,
                            "role": None,
                            "generation": None,
                            "session_fingerprint": fp or None,
                            "state": "TERMINAL_SESSION_RESULT_NOT_PERSISTED" if identity else "RESULT_IDENTITY_UNRESOLVED",
                            "age_seconds": _age_seconds(observed_at, now=now),
                            "exact_cause": cause,
                            "safe_recovery_available": recovery,
                            "parent_action_required": False,
                            "ues_automatic_recovery_possible": recovery,
                            "blocking_scope": "LANE",
                        })

    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in entries:
        key = (
            str(entry.get("project") or ""),
            str(entry.get("lane_id") or ""),
            str(entry.get("session_fingerprint") or ""),
        )
        prior = deduped.get(key)
        if prior is None or str(prior.get("state")) != "TERMINAL_RESULT_PARENT_CONSUMABLE":
            deduped[key] = entry
    lanes = sorted(
        deduped.values(),
        key=lambda item: (
            str(item.get("project") or ""),
            str(item.get("logical_workstream") or ""),
            str(item.get("role") or ""),
            int(item.get("generation") or 0),
        ),
    )
    counts = Counter(str(item.get("state") or "UNKNOWN") for item in lanes)
    blocked = [item for item in lanes if item.get("state") != "TERMINAL_RESULT_PARENT_CONSUMABLE"]
    recoverable = [item for item in blocked if item.get("ues_automatic_recovery_possible")]
    parent = [item for item in lanes if item.get("parent_action_required")]
    return {
        "schema_version": "1.0",
        "result": "TERMINAL_LIVENESS_WATCHDOG_AUDIT",
        "cycle_status": "CONTROL_CYCLE_FAILED" if blocked else "CONTROL_CYCLE_OK",
        "lane_count": len(lanes),
        "state_counts": dict(sorted(counts.items())),
        "blocked_lane_count": len(blocked),
        "recoverable_lane_count": len(recoverable),
        "parent_action_lane_count": len(parent),
        "lanes": lanes,
        "blocked_lane_freezes_independent_lanes": False,
        "blocking_scope_default": "LANE",
        "mutation_performed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES terminal-result liveness watchdog")
    parser.parse_args(argv)
    result = run()
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("cycle_status") == "CONTROL_CYCLE_OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
