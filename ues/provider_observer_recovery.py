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
RECOVERY_WORKSTREAM = "PROVIDER-OBSERVER-HEALTH"
RECOVERY_LEASE_TTL_SECONDS = 5 * 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _health_lane_id() -> str:
    return canonical_lane_id("UES", "INTERNAL:UES", RECOVERY_WORKSTREAM)


def _ensure_health_lane(store: StateStore) -> None:
    lane_id = _health_lane_id()
    current = store.read_workstream(lane_id)
    if current.status == "OK" and current.record is not None:
        return
    if current.status != "MISSING":
        raise StateUnavailable(current.reason or "provider observer health lane unavailable")
    record = WorkstreamRuntimeRecord(
        lane_id=lane_id,
        project="UES",
        route="INTERNAL:UES",
        workstream_id=RECOVERY_WORKSTREAM,
        activation_mode="SHADOW",
        authority_provenance={
            "scope": "READ_ONLY_PROVIDER_OBSERVER_RECOVERY_COORDINATION",
            "provider_mutation_authorized": False,
        },
    )
    store.compare_and_swap_workstream(lane_id, 0, record)


def recover_if_stale(
    *,
    now: datetime | None = None,
    stale_seconds: int | None = None,
) -> dict[str, Any]:
    stale_seconds = stale_seconds if stale_seconds is not None else _configured_stale_seconds()
    store = build_live_state_store()
    before = freshness_snapshot(store, now=now, stale_seconds=stale_seconds)
    if not before["recovery_required"]:
        return {
            "result": "PROVIDER_OBSERVER_FALLBACK_NOT_NEEDED",
            "before": before,
            "provider_mutation_performed": False,
        }

    _ensure_health_lane(store)
    lane_id = _health_lane_id()
    operation_key = "ues-v2:provider-observer-fallback:" + sha256(
        b"provider-observer-fallback-v1"
    ).hexdigest()
    try:
        lease = store.acquire_lease(
            lane_id,
            RECOVERY_OWNER,
            operation_key,
            RECOVERY_LEASE_TTL_SECONDS,
            now=now,
        )
    except LeaseCollision:
        return {
            "result": "PROVIDER_OBSERVER_FALLBACK_ALREADY_IN_FLIGHT",
            "before": before,
            "provider_mutation_performed": False,
        }

    try:
        after_lease = freshness_snapshot(store, now=now, stale_seconds=stale_seconds)
        if not after_lease["recovery_required"]:
            return {
                "result": "PROVIDER_OBSERVER_FALLBACK_SUPERSEDED_BY_FRESH_READBACK",
                "before": before,
                "after_lease": after_lease,
                "provider_mutation_performed": False,
            }

        observation = observe()
        return {
            "result": (
                "PROVIDER_OBSERVER_FALLBACK_RECOVERED"
                if observation.get("result") == "JULES_PROVIDER_OBSERVATION_COMPLETE"
                else "PROVIDER_OBSERVER_FALLBACK_FAILED"
            ),
            "before": before,
            "observation": observation,
            "provider_mutation_performed": False,
        }
    finally:
        store.release_lease(lane_id, lease.lease.lease_id)


def main() -> int:
    result = recover_if_stale()
    print(json.dumps(result, sort_keys=True))
    return 2 if result.get("result") == "PROVIDER_OBSERVER_FALLBACK_FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
