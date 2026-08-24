from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from .identity import canonical_lane_id
from .state_store import StateUnavailable, StateVersionConflict, WorkstreamRuntimeRecord


ALLOWED_WAKEUP_TYPES = frozenset(
    {
        "PROVIDER_SESSION_TRANSITION",
        "PROVIDER_ACTIVITY",
        "PR_HEAD_MOVED",
        "CI_COMPLETED",
        "ARTIFACT_AVAILABLE",
        "REVIEWER_COMPLETED",
        "WRITER_COMPLETED",
        "CORRECTION_COMMIT",
        "WORKFLOW_COMPLETED",
        "STATESTORE_TRANSITION",
        "EXTERNAL_RECONCILIATION_REQUEST",
    }
)
EVENT_WORKSTREAM = "LIFECYCLE-EVENT-INGRESS"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def event_fingerprint(event: Mapping[str, Any]) -> str:
    event_type = str(event.get("type") or "").strip().upper()
    event_id = str(event.get("event_id") or "").strip()
    if event_type not in ALLOWED_WAKEUP_TYPES:
        raise ValueError("event type is not allowlisted")
    if not event_id:
        raise ValueError("event_id is required")
    payload = {
        "type": event_type,
        "event_id": event_id,
        "source": str(event.get("source") or "").strip(),
        "repository": str(event.get("repository") or "").strip(),
        "workstream": str(event.get("workstream") or "").strip(),
        "sha": str(event.get("sha") or "").strip().lower(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def register_wakeup(
    store: Any,
    *,
    project: str,
    route: str,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    """Coalesce one event durably. An event only grants a wakeup, never authority."""

    fingerprint = event_fingerprint(event)
    lane_id = canonical_lane_id(project, route, EVENT_WORKSTREAM)
    for attempt in range(3):
        read = store.read_workstream(lane_id)
        if read.status == "MISSING":
            record = WorkstreamRuntimeRecord(
                lane_id=lane_id,
                project=project,
                route=route,
                workstream_id=EVENT_WORKSTREAM,
                activation_mode="SHADOW",
            )
            expected = 0
            seen: list[str] = []
        elif read.status == "OK" and read.record is not None:
            record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
            expected = read.version
            evidence = record.evidence_bindings or {}
            raw_seen = evidence.get("recent_event_fingerprints") or []
            seen = [str(item) for item in raw_seen if str(item)] if isinstance(raw_seen, list) else []
        else:
            raise StateUnavailable(read.reason or "event ingress state unavailable")

        if fingerprint in seen:
            return {
                "schema_version": "1.0",
                "decision": "DUPLICATE_EVENT_COALESCED",
                "wakeup": False,
                "event_fingerprint": fingerprint,
                "event_grants_mutation_authority": False,
            }

        recent = (seen + [fingerprint])[-128:]
        record.activation_mode = "SHADOW"
        record.actor_bindings = {}
        record.authority_provenance = {
            "scope": "EVENT_WAKEUP_INGRESS",
            "event_grants_mutation_authority": False,
            "authority_reconstruction_required_after_wakeup": True,
        }
        record.evidence_bindings = {
            "recent_event_fingerprints": recent,
            "last_event_fingerprint": fingerprint,
            "last_event_type": str(event.get("type") or "").upper(),
            "last_event_id": str(event.get("event_id") or ""),
            "last_event_source": str(event.get("source") or ""),
            "last_event_repository": str(event.get("repository") or ""),
            "last_event_workstream": str(event.get("workstream") or ""),
            "last_event_sha": str(event.get("sha") or "").lower() or None,
            "registered_at": _iso_now(),
        }
        record.last_successful_transition = {
            "kind": "EVENT_WAKEUP_REGISTERED",
            "event_fingerprint": fingerprint,
            "at": _iso_now(),
        }
        try:
            saved = store.compare_and_swap_workstream(lane_id, expected, record)
        except StateVersionConflict:
            if attempt < 2:
                continue
            raise
        if saved.status != "OK":
            raise StateUnavailable(saved.reason or "event wakeup persistence failed")
        return {
            "schema_version": "1.0",
            "decision": "NEW_EVENT_WAKEUP_REGISTERED",
            "wakeup": True,
            "event_fingerprint": fingerprint,
            "event_grants_mutation_authority": False,
            "lane_id": lane_id,
            "version": saved.version,
        }
    raise StateUnavailable("event wakeup registration exhausted CAS attempts")
