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


def _seen_fingerprints(read: Any) -> list[str]:
    if read.status == "MISSING":
        return []
    if read.status == "OK" and read.record is not None:
        evidence = read.record.evidence_bindings or {}
        raw_seen = evidence.get("recent_event_fingerprints") or []
        return [str(item) for item in raw_seen if str(item)] if isinstance(raw_seen, list) else []
    raise StateUnavailable(read.reason or "event ingress state unavailable")


def _duplicate_event_result(
    *,
    event_type: str,
    fingerprint: str,
    after_cas_contention: bool = False,
) -> dict[str, Any]:
    """Classify a durably seen event without granting effect authority.

    Ordinary provider/CI events are safe to coalesce because the event is only a
    wakeup accelerator. An EXTERNAL_RECONCILIATION_REQUEST is different: current
    project authority, exact candidate SHA, lineage generation, or predecessor
    binding may have changed since the same transport event was first observed.
    In that case the durable duplicate proves only that the wakeup was seen; it
    must not suppress a fresh guarded lifecycle reconciliation. Downstream exact
    authority, generation-transition identity, duplicate/UNKNOWN, idempotency,
    quota, and provider gates remain authoritative and fail closed.
    """

    if event_type == "EXTERNAL_RECONCILIATION_REQUEST":
        decision = "DUPLICATE_EXTERNAL_RECONCILIATION_CONTINUE_GUARDED"
        if after_cas_contention:
            decision += "_AFTER_CAS_CONTENTION"
        return {
            "schema_version": "1.0",
            "decision": decision,
            "wakeup": True,
            "event_fingerprint": fingerprint,
            "event_grants_mutation_authority": False,
            "coalescing_durable": True,
            "downstream_authority_reconstruction_required": True,
            "downstream_idempotency_and_unknown_checks_required": True,
            "safe_to_blind_retry": False,
        }

    return {
        "schema_version": "1.0",
        "decision": (
            "DUPLICATE_EVENT_COALESCED_AFTER_CAS_CONTENTION"
            if after_cas_contention
            else "DUPLICATE_EVENT_COALESCED"
        ),
        "wakeup": False,
        "event_fingerprint": fingerprint,
        "event_grants_mutation_authority": False,
        "coalescing_durable": True,
        "safe_to_blind_retry": False,
    }


def _degraded_wakeup_after_cas_exhaustion(
    store: Any,
    *,
    lane_id: str,
    fingerprint: str,
    event_type: str,
) -> dict[str, Any]:
    """Final authoritative readback after bounded event-coalescing CAS contention.

    The event-ingress lane is only a duplicate-coalescing accelerator and never an
    authority or effect receipt. If a final authoritative read proves that the
    requested fingerprint is not durably registered, the already-triggered control
    cycle may continue under its normal downstream authority, duplicate/UNKNOWN,
    idempotency, StateStore, and provider-effect gates. We never retry a provider
    effect here and we never claim durable event coalescing when it was not proven.
    """

    read = store.read_workstream(lane_id)
    seen = _seen_fingerprints(read)
    if fingerprint in seen:
        return _duplicate_event_result(
            event_type=event_type,
            fingerprint=fingerprint,
            after_cas_contention=True,
        )
    return {
        "schema_version": "1.0",
        "decision": "EVENT_WAKEUP_COALESCING_NOT_DURABLE_CONTINUE_GUARDED",
        "wakeup": True,
        "event_fingerprint": fingerprint,
        "event_grants_mutation_authority": False,
        "coalescing_durable": False,
        "downstream_authority_reconstruction_required": True,
        "downstream_idempotency_and_unknown_checks_required": True,
        "safe_to_blind_retry": False,
        "lane_id": lane_id,
        "observed_event_lane_status": read.status,
        "observed_event_lane_version": int(getattr(read, "version", 0) or 0),
    }


def register_wakeup(
    store: Any,
    *,
    project: str,
    route: str,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    """Coalesce one event durably when possible. An event only grants a wakeup, never authority."""

    fingerprint = event_fingerprint(event)
    event_type = str(event.get("type") or "").strip().upper()
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
            seen = _seen_fingerprints(read)
        else:
            raise StateUnavailable(read.reason or "event ingress state unavailable")

        if fingerprint in seen:
            return _duplicate_event_result(
                event_type=event_type,
                fingerprint=fingerprint,
            )

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
            "last_event_type": event_type,
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
            return _degraded_wakeup_after_cas_exhaustion(
                store,
                lane_id=lane_id,
                fingerprint=fingerprint,
                event_type=event_type,
            )
        if saved.status != "OK":
            raise StateUnavailable(saved.reason or "event wakeup persistence failed")
        return {
            "schema_version": "1.0",
            "decision": "NEW_EVENT_WAKEUP_REGISTERED",
            "wakeup": True,
            "event_fingerprint": fingerprint,
            "event_grants_mutation_authority": False,
            "coalescing_durable": True,
            "safe_to_blind_retry": False,
            "lane_id": lane_id,
            "version": saved.version,
        }
    raise StateUnavailable("event wakeup registration exhausted CAS attempts")
