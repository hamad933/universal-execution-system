from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

from .bounded_waiting_runtime import run as run_bounded_waiting
from .identity import canonical_lane_id
from .live_runtime import build_live_state_store
from .state_store import StateUnavailable, WorkstreamRuntimeRecord

SCHEMA_VERSION = "1.0"
HEALTH_WORKSTREAM = "BOUNDED-EXISTING-SESSION-HEALTH"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _lane_id() -> str:
    return canonical_lane_id("UES", "INTERNAL:UES", HEALTH_WORKSTREAM)


def _summary(result: Mapping[str, Any]) -> dict[str, Any]:
    items = result.get("results")
    rows = [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []
    decisions = Counter(str(item.get("decision") or "UNKNOWN") for item in rows)
    workstreams = sorted({str(item.get("workstream") or "") for item in rows if item.get("workstream")})
    return {
        "project": str(result.get("project") or ""),
        "result": str(result.get("result") or "UNKNOWN"),
        "decision_counts": dict(sorted(decisions.items())),
        "workstreams_observed": workstreams,
        "external_effects_dispatched": int(result.get("external_effects_dispatched") or 0),
        "new_tasks_or_sessions_created": int(result.get("new_tasks_or_sessions_created") or 0),
        "raw_session_ids_persisted": False,
        "activity_content_persisted": False,
    }


def persist_cycle(*, phase: str, status: str, summary: Mapping[str, Any] | None = None, error_category: str | None = None) -> dict[str, Any]:
    store = build_live_state_store()
    lane_id = _lane_id()
    read = store.read_workstream(lane_id)
    if read.status == "MISSING":
        record = WorkstreamRuntimeRecord(
            lane_id=lane_id,
            project="UES",
            route="INTERNAL:UES",
            workstream_id=HEALTH_WORKSTREAM,
            activation_mode="SHADOW",
        )
        expected = 0
    elif read.status == "OK" and read.record is not None:
        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        expected = read.version
    else:
        raise StateUnavailable(read.reason or "bounded waiting health lane unavailable")

    now = _iso_now()
    record.activation_mode = "SHADOW"
    record.actor_bindings = {}
    record.authority_provenance = {
        "scope": "BOUNDED_EXISTING_SESSION_CYCLE_TELEMETRY",
        "provider_mutation_authority_source": "PROJECT_BOUNDED_RUNTIME_POLICY",
        "telemetry_grants_no_authority": True,
        "raw_provider_identity_persisted": False,
    }
    record.last_observed_provider_state = {
        "phase": phase,
        "status": status,
        "error_category": error_category,
        "cycle_summary": dict(summary or {}),
        "exception_text_persisted": False,
    }
    record.last_successful_transition = {
        "kind": "BOUNDED_EXISTING_SESSION_CYCLE",
        "phase": phase,
        "status": status,
        "at": now,
    }
    saved = store.compare_and_swap_workstream(lane_id, expected, record)
    if saved.status != "OK" or saved.record is None:
        raise StateUnavailable(saved.reason or "bounded waiting health persistence failed")
    return {"lane_id": lane_id, "version": saved.version, "phase": phase, "status": status}


def run_supervised(project: str) -> dict[str, Any]:
    persist_cycle(phase="START", status="IN_FLIGHT")
    try:
        result = run_bounded_waiting(project)
        summary = _summary(result)
        health = persist_cycle(phase="COMPLETE", status="PASS", summary=summary)
        return {
            "schema_version": SCHEMA_VERSION,
            "result": "BOUNDED_EXISTING_SESSION_SUPERVISED_COMPLETE",
            "cycle_summary": summary,
            "health": health,
        }
    except Exception as exc:
        category = str(getattr(exc, "category", None) or type(exc).__name__).upper()[:120]
        try:
            health = persist_cycle(phase="FAILED", status="FAIL", error_category=category)
        except Exception:
            health = {"phase": "FAILED", "status": "FAIL", "error_category": category, "health_persistence": "FAILED"}
        return {
            "schema_version": SCHEMA_VERSION,
            "result": "BOUNDED_EXISTING_SESSION_SUPERVISED_FAILED",
            "error_category": category,
            "exception_text_persisted": False,
            "health": health,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervise bounded existing-session continuation with durable cycle telemetry")
    parser.add_argument("project", choices=("GS", "CEP"))
    args = parser.parse_args(argv)
    result = run_supervised(args.project)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("result") == "BOUNDED_EXISTING_SESSION_SUPERVISED_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
