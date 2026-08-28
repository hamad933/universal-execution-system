from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from .identity import canonical_lane_id
from .live_runtime import build_live_state_store
from .provider_observer import PROJECTS, observation_lane_id
from .provider_observer_runtime import observe
from .state_store import LeaseCollision, StateStore, StateUnavailable, WorkstreamRuntimeRecord

DEFAULT_STALE_SECONDS = 20 * 60
RECOVERY_OWNER = "ues-provider-observer-fallback"
RECOVERY_COORDINATION_WORKSTREAM = "PROVIDER-OBSERVER-RECOVERY-COORDINATION"
RECOVERY_HEALTH_WORKSTREAM = "PROVIDER-OBSERVER-FALLBACK-HEALTH"
RECOVERY_LEASE_TTL_SECONDS = 5 * 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
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


def _configured_stale_seconds() -> int:
    raw = str(os.environ.get("UES_PROVIDER_OBSERVER_FALLBACK_STALE_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_STALE_SECONDS
    value = int(raw)
    if value <= 0:
        raise ValueError("UES_PROVIDER_OBSERVER_FALLBACK_STALE_SECONDS must be positive")
    return value


def _trigger_snapshot() -> dict[str, str | None]:
    return {
        "event_name": str(os.environ.get("GITHUB_EVENT_NAME") or "").strip() or None,
        "run_id": str(os.environ.get("GITHUB_RUN_ID") or "").strip() or None,
        "run_attempt": str(os.environ.get("GITHUB_RUN_ATTEMPT") or "").strip() or None,
        "sha": str(os.environ.get("GITHUB_SHA") or "").strip() or None,
        "ref": str(os.environ.get("GITHUB_REF") or "").strip() or None,
    }


def freshness_snapshot(
    store: StateStore,
    *,
    now: datetime | None = None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
) -> dict[str, Any]:
    if stale_seconds <= 0:
        raise ValueError("stale_seconds must be positive")
    now = (now or _utc_now()).astimezone(timezone.utc)
    projects: list[dict[str, Any]] = []
    recovery_required = False

    for project in PROJECTS:
        lane_id = observation_lane_id(project)
        read = store.read_workstream(lane_id)
        observed_at: datetime | None = None
        reason: str | None = None
        if read.status == "OK" and read.record is not None:
            provider_state = read.record.last_observed_provider_state or {}
            observed_at = _parse_time(provider_state.get("observed_at"))
            if observed_at is None:
                reason = "OBSERVATION_TIMESTAMP_MISSING_OR_INVALID"
        elif read.status == "MISSING":
            reason = "OBSERVATION_MISSING"
        else:
            reason = "OBSERVATION_STATE_UNAVAILABLE"

        age_seconds = None if observed_at is None else max(0, int((now - observed_at).total_seconds()))
        stale = observed_at is None or bool(age_seconds is not None and age_seconds > stale_seconds)
        if stale and reason is None:
            reason = "OBSERVATION_STALE"
        recovery_required = recovery_required or stale
        projects.append(
            {
                "project": project["project"],
                "lane_id": lane_id,
                "state_status": read.status,
                "version": read.version,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z") if observed_at else None,
                "age_seconds": age_seconds,
                "stale": stale,
                "reason": reason,
            }
        )

    return {
        "result": "PROVIDER_OBSERVER_FRESHNESS_CHECK",
        "stale_seconds": stale_seconds,
        "recovery_required": recovery_required,
        "projects": projects,
        "provider_mutation_performed": False,
    }


def _coordination_lane_id() -> str:
    return canonical_lane_id("UES", "INTERNAL:UES", RECOVERY_COORDINATION_WORKSTREAM)


def _health_lane_id() -> str:
    return canonical_lane_id("UES", "INTERNAL:UES", RECOVERY_HEALTH_WORKSTREAM)


def _ensure_lane(store: StateStore, *, lane_id: str, workstream_id: str, scope: str) -> None:
    current = store.read_workstream(lane_id)
    if current.status == "OK" and current.record is not None:
        return
    if current.status != "MISSING":
        raise StateUnavailable(current.reason or f"{workstream_id} lane unavailable")
    record = WorkstreamRuntimeRecord(
        lane_id=lane_id,
        project="UES",
        route="INTERNAL:UES",
        workstream_id=workstream_id,
        activation_mode="SHADOW",
        authority_provenance={
            "scope": scope,
            "provider_mutation_authorized": False,
        },
    )
    store.compare_and_swap_workstream(lane_id, 0, record)


def _persist_fallback_health(
    store: StateStore,
    *,
    phase: str,
    status: str,
    result: str,
    snapshot: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or _utc_now()).astimezone(timezone.utc)
    lane_id = _health_lane_id()
    _ensure_lane(
        store,
        lane_id=lane_id,
        workstream_id=RECOVERY_HEALTH_WORKSTREAM,
        scope="READ_ONLY_PROVIDER_OBSERVER_FALLBACK_TELEMETRY",
    )
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable(read.reason or "provider observer fallback health lane unavailable")
    record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
    record.activation_mode = "SHADOW"
    record.actor_bindings = {}
    record.authority_provenance = {
        "scope": "READ_ONLY_PROVIDER_OBSERVER_FALLBACK_TELEMETRY",
        "provider_mutation_authorized": False,
    }
    record.last_observed_provider_state = {
        "phase": phase,
        "status": status,
        "result": result,
        "checked_at": _iso(now),
        "trigger": _trigger_snapshot(),
        "stale_seconds": snapshot.get("stale_seconds"),
        "recovery_required": bool(snapshot.get("recovery_required")),
        "projects": [
            {
                "project": item.get("project"),
                "version": item.get("version"),
                "observed_at": item.get("observed_at"),
                "age_seconds": item.get("age_seconds"),
                "stale": item.get("stale"),
                "reason": item.get("reason"),
            }
            for item in snapshot.get("projects", [])
        ],
        "provider_mutation_performed": False,
        "unknown_write_state": None,
    }
    record.last_successful_transition = {
        "kind": "PROVIDER_OBSERVER_FALLBACK_HEALTH",
        "phase": phase,
        "status": status,
        "result": result,
        "at": _iso(now),
    }
    saved = store.compare_and_swap_workstream(lane_id, read.version, record)
    return {
        "lane_id": lane_id,
        "version": saved.version,
        "phase": phase,
        "status": status,
        "result": result,
        "checked_at": _iso(now),
        "provider_mutation_performed": False,
    }


def recover_if_stale(
    *,
    now: datetime | None = None,
    stale_seconds: int | None = None,
) -> dict[str, Any]:
    now = (now or _utc_now()).astimezone(timezone.utc)
    stale_seconds = stale_seconds if stale_seconds is not None else _configured_stale_seconds()
    store = build_live_state_store()
    before = freshness_snapshot(store, now=now, stale_seconds=stale_seconds)

    if not before["recovery_required"]:
        health = _persist_fallback_health(
            store,
            phase="COMPLETE",
            status="PASS",
            result="PROVIDER_OBSERVER_FALLBACK_NOT_NEEDED",
            snapshot=before,
            now=now,
        )
        return {
            "result": "PROVIDER_OBSERVER_FALLBACK_NOT_NEEDED",
            "before": before,
            "health": health,
            "provider_mutation_performed": False,
        }

    coordination_lane = _coordination_lane_id()
    _ensure_lane(
        store,
        lane_id=coordination_lane,
        workstream_id=RECOVERY_COORDINATION_WORKSTREAM,
        scope="READ_ONLY_PROVIDER_OBSERVER_RECOVERY_COORDINATION",
    )
    operation_key = "ues-v2:provider-observer-fallback:" + sha256(
        b"provider-observer-fallback-v2"
    ).hexdigest()
    try:
        lease = store.acquire_lease(
            coordination_lane,
            RECOVERY_OWNER,
            operation_key,
            RECOVERY_LEASE_TTL_SECONDS,
            now=now,
        )
    except LeaseCollision:
        health = _persist_fallback_health(
            store,
            phase="DEFERRED",
            status="PASS",
            result="PROVIDER_OBSERVER_FALLBACK_ALREADY_IN_FLIGHT",
            snapshot=before,
            now=now,
        )
        return {
            "result": "PROVIDER_OBSERVER_FALLBACK_ALREADY_IN_FLIGHT",
            "before": before,
            "health": health,
            "provider_mutation_performed": False,
        }

    try:
        after_lease = freshness_snapshot(store, now=now, stale_seconds=stale_seconds)
        if not after_lease["recovery_required"]:
            outcome = "PROVIDER_OBSERVER_FALLBACK_SUPERSEDED_BY_FRESH_READBACK"
            snapshot = after_lease
            observation = None
        else:
            observation = observe()
            snapshot = freshness_snapshot(store, now=now, stale_seconds=stale_seconds)
            if observation.get("result") == "JULES_PROVIDER_OBSERVATION_COMPLETE":
                outcome = "PROVIDER_OBSERVER_FALLBACK_RECOVERED"
            elif not snapshot["recovery_required"]:
                # The local fallback may lose a transient provider race while a
                # concurrent canonical observer has already persisted the fresh
                # authoritative state. The post-readback, not the local attempt,
                # owns the final liveness decision.
                outcome = "PROVIDER_OBSERVER_FALLBACK_SUPERSEDED_BY_FRESH_READBACK"
            else:
                outcome = "PROVIDER_OBSERVER_FALLBACK_FAILED"
    finally:
        store.release_lease(coordination_lane, lease.lease.lease_id)

    health = _persist_fallback_health(
        store,
        phase="COMPLETE" if outcome != "PROVIDER_OBSERVER_FALLBACK_FAILED" else "FAILED",
        status="PASS" if outcome != "PROVIDER_OBSERVER_FALLBACK_FAILED" else "FAIL",
        result=outcome,
        snapshot=snapshot,
        now=now,
    )
    result: dict[str, Any] = {
        "result": outcome,
        "before": before,
        "health": health,
        "provider_mutation_performed": False,
    }
    if outcome == "PROVIDER_OBSERVER_FALLBACK_SUPERSEDED_BY_FRESH_READBACK":
        result["after_lease"] = snapshot
        if observation is not None:
            result["observation"] = observation
            result["after"] = snapshot
    elif observation is not None:
        result["observation"] = observation
        result["after"] = snapshot
    return result


def main() -> int:
    result = recover_if_stale()
    print(json.dumps(result, sort_keys=True))
    return 2 if result.get("result") == "PROVIDER_OBSERVER_FALLBACK_FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
