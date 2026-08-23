from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping

from .identity import canonical_lane_id
from .providers.base import ProtocolError
from .providers.jules import JulesClient
from .state_backends.public_same_repo import (
    OWNER_AUTHORIZED_PUBLIC_SAME_REPO_POLICY,
    OwnerAuthorizedSameRepoGitDataTransport,
    OwnerAuthorizedSameRepoStateStore,
)
from .state_store import (
    OperationRecord,
    StateUnavailable,
    StateVersionConflict,
    WorkstreamRuntimeRecord,
    production_state_store_assessment,
)
from .watchdog import evaluate_control_cycle

SCHEMA_VERSION = "2.0"
PROBE_PROJECT = "UES"
PROBE_ROUTE = "INTERNAL:UES"
PROBE_WORKSTREAM = "LIVE-RUNTIME-PROBE"
PROBE_AUTHORITY_EVENT = "UES_OWNER_AUTHORIZED_SAME_REPO_STATESTORE_2026_08_24"


def _env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


class BoundedJulesProbeClient(JulesClient):
    """One-page, read-only Jules credential probe; never calls a mutation endpoint."""

    def probe_authentication(self) -> dict[str, Any]:
        payload = self._read_json(
            "/v1alpha/sessions?pageSize=1",
            operation="jules.sessions.readOnlyProbe",
        )
        if not isinstance(payload, Mapping):
            raise ProtocolError(
                "Jules authentication probe response must be an object",
                operation="jules.sessions.readOnlyProbe",
            )
        sessions = payload.get("sessions", [])
        if not isinstance(sessions, list):
            raise ProtocolError(
                "Jules authentication probe sessions field must be a list",
                operation="jules.sessions.readOnlyProbe",
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "provider": "JULES",
            "authenticated_read_succeeded": True,
            "read_only": True,
            "mutation_performed": False,
            "page_size_requested": 1,
            "items_observed_on_page": len(sessions),
        }


def build_live_state_store() -> OwnerAuthorizedSameRepoStateStore:
    if str(os.environ.get("UES_ALLOW_PUBLIC_SAME_REPO_STATE") or "").strip().lower() != "true":
        raise RuntimeError(
            "UES_ALLOW_PUBLIC_SAME_REPO_STATE=true is required for the explicit owner-authorized policy"
        )
    repository = _env("GITHUB_REPOSITORY")
    token = _env("GITHUB_TOKEN")
    prefix = str(os.environ.get("UES_STATE_REF_PREFIX") or "ues-runtime/v2").strip()
    transport = OwnerAuthorizedSameRepoGitDataTransport(
        repository,
        token,
        expected_repository=repository,
    )
    return OwnerAuthorizedSameRepoStateStore(transport, ref_prefix=prefix)


def _fresh_record(lane_id: str) -> WorkstreamRuntimeRecord:
    return WorkstreamRuntimeRecord(
        lane_id=lane_id,
        project=PROBE_PROJECT,
        route=PROBE_ROUTE,
        workstream_id=PROBE_WORKSTREAM,
        activation_mode="SHADOW",
        authority_provenance={
            "authority_event_id": PROBE_AUTHORITY_EVENT,
            "storage_policy": OWNER_AUTHORIZED_PUBLIC_SAME_REPO_POLICY,
            "scope": "UES_RUNTIME_STATE_ONLY",
            "provider_mutation_authorized": False,
        },
        evidence_bindings={
            "probe": "LIVE_READ_WRITE_RECOVERY_CAS_LEASE",
            "secret_material_persisted": False,
        },
    )


def run_state_smoke() -> dict[str, Any]:
    """Perform a live same-repo StateStore proof without touching project/provider state."""

    repository = _env("GITHUB_REPOSITORY")
    store_a = build_live_state_store()
    assessment = production_state_store_assessment(store_a)
    if not assessment["ready_for_cross_run_production"]:
        raise RuntimeError("live StateStore does not satisfy required production capabilities")

    lane_id = canonical_lane_id(PROBE_PROJECT, PROBE_ROUTE, PROBE_WORKSTREAM)
    current = store_a.read_workstream(lane_id)
    if current.status == "MISSING":
        saved = store_a.compare_and_swap_workstream(lane_id, 0, _fresh_record(lane_id))
    elif current.status == "OK" and current.record is not None:
        record = WorkstreamRuntimeRecord.from_dict(current.record.to_dict())
        record.activation_mode = "SHADOW"
        record.authority_provenance = {
            "authority_event_id": PROBE_AUTHORITY_EVENT,
            "storage_policy": OWNER_AUTHORIZED_PUBLIC_SAME_REPO_POLICY,
            "scope": "UES_RUNTIME_STATE_ONLY",
            "provider_mutation_authorized": False,
        }
        saved = store_a.compare_and_swap_workstream(lane_id, current.version, record)
    else:
        raise StateUnavailable(current.reason or f"unexpected live lane status: {current.status}")

    store_b = build_live_state_store()
    recovered = store_b.read_workstream(lane_id)
    if recovered.status != "OK" or recovered.record is None:
        raise StateUnavailable(recovered.reason or "live StateStore recovery failed")
    if recovered.version != saved.version:
        raise StateUnavailable("recovered live StateStore version differs from committed version")

    stale_version = recovered.version
    stale_record = WorkstreamRuntimeRecord.from_dict(recovered.record.to_dict())
    winner_record = WorkstreamRuntimeRecord.from_dict(recovered.record.to_dict())
    winner_record.last_successful_transition = {
        "kind": "LIVE_STATESTORE_SMOKE",
        "result": "READ_WRITE_RECOVERY_PASS",
        "at": _iso(_utc_now()),
    }
    winner = store_b.compare_and_swap_workstream(lane_id, stale_version, winner_record)
    stale_record.last_successful_transition = {
        "kind": "LIVE_STATESTORE_SMOKE",
        "result": "STALE_WRITE_MUST_NOT_WIN",
    }
    conflict_proven = False
    try:
        store_a.compare_and_swap_workstream(lane_id, stale_version, stale_record)
    except StateVersionConflict:
        conflict_proven = True
    if not conflict_proven:
        raise RuntimeError("live StateStore stale CAS write was not rejected")

    operation_key = "ues-v2:state-smoke:" + sha256(
        f"{repository}|{lane_id}".encode("utf-8")
    ).hexdigest()
    lease_result = store_b.acquire_lease(
        lane_id,
        "ues-live-runtime-smoke",
        operation_key,
        120,
    )
    store_c = build_live_state_store()
    leased = store_c.read_workstream(lane_id)
    if leased.status != "OK" or leased.record is None or leased.record.lease is None:
        raise StateUnavailable("live lane lease was not durable across backend reconstruction")
    if leased.record.lease.lease_id != lease_result.lease.lease_id:
        raise StateUnavailable("live lane lease identity changed across backend reconstruction")
    released = store_c.release_lease(lane_id, lease_result.lease.lease_id)
    if released.record is None or released.record.lease is not None:
        raise StateUnavailable("live lane lease release did not persist")

    operation_read = store_c.read_operation(operation_key)
    if operation_read.status == "MISSING":
        now = _iso(_utc_now())
        operation = OperationRecord(
            operation_key=operation_key,
            lane_id=lane_id,
            workstream_id=PROBE_WORKSTREAM,
            action="state-store-smoke",
            request_digest=sha256(PROBE_AUTHORITY_EVENT.encode("utf-8")).hexdigest(),
            state="CONFIRMED",
            owner="ues-live-runtime-smoke",
            started_at=now,
            updated_at=now,
            receipt={
                "result": "LIVE_STATESTORE_PROOF_PASS",
                "provider_mutation": False,
                "secret_material_persisted": False,
            },
        )
        operation_read = store_c.compare_and_swap_operation(operation_key, 0, operation)
    if operation_read.status != "OK" or operation_read.record is None:
        raise StateUnavailable(operation_read.reason or "live operation record is unavailable")
    if operation_read.record.state != "CONFIRMED":
        raise StateUnavailable("live smoke operation is not confirmed")

    store_d = build_live_state_store()
    recovered_operation = store_d.read_operation(operation_key)
    if recovered_operation.status != "OK" or recovered_operation.record is None:
        raise StateUnavailable("live operation record did not survive backend reconstruction")

    discovered_lanes = store_d.discover_lane_ids()
    discovered_operations = store_d.discover_operation_keys()
    if lane_id not in discovered_lanes or operation_key not in discovered_operations:
        raise StateUnavailable("live StateStore discovery did not recover the probe identities")

    return {
        "schema_version": SCHEMA_VERSION,
        "result": "LIVE_STATESTORE_READ_WRITE_RECOVERY_PASS",
        "repository": repository,
        "storage_visibility": store_d.transport.storage_visibility,
        "storage_policy": OWNER_AUTHORIZED_PUBLIC_SAME_REPO_POLICY,
        "backend_name": store_d.capabilities.backend_name,
        "lane_id": lane_id,
        "final_lane_version": store_d.read_workstream(lane_id).version,
        "operation_state": recovered_operation.record.state,
        "runner_replacement_recovery": True,
        "cas_conflict_rejected": True,
        "lane_lease_cross_run_proven": True,
        "identity_discovery_proven": True,
        "provider_mutation_performed": False,
        "secret_material_persisted": False,
    }


def run_state_audit() -> dict[str, Any]:
    """Read-only runtime watchdog over all durable lanes/operations in this namespace."""

    store = build_live_state_store()
    lane_ids = store.discover_lane_ids()
    operation_keys = store.discover_operation_keys()
    unresolved_by_lane: dict[str, list[str]] = {}
    operation_state_counts: dict[str, int] = {}

    for key in operation_keys:
        read = store.read_operation(key)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or "runtime operation audit read failed")
        state = read.record.state
        operation_state_counts[state] = operation_state_counts.get(state, 0) + 1
        if state in {"IN_FLIGHT", "UNKNOWN"}:
            unresolved_by_lane.setdefault(read.record.lane_id, []).append(state)

    watchdog_lanes: list[dict[str, Any]] = []
    lane_status_counts: dict[str, int] = {}
    now = _utc_now()
    for lane_id in lane_ids:
        read = store.read_workstream(lane_id)
        lane_status_counts[read.status] = lane_status_counts.get(read.status, 0) + 1
        stop_gate: str | None = None
        proven_incident = False
        terminal_failed = False
        if read.status != "OK" or read.record is None:
            stop_gate = "RUNTIME_STATE_UNAVAILABLE"
            proven_incident = True
        else:
            record = read.record
            if record.unknown_write_state or unresolved_by_lane.get(lane_id):
                stop_gate = "AUTHORITATIVE_RECONCILIATION_REQUIRED"
                proven_incident = True
            if record.lease is not None:
                try:
                    if _parse_time(record.lease.expires_at) <= now:
                        stop_gate = stop_gate or "STALE_LEASE_REQUIRES_RECONCILIATION"
                        proven_incident = True
                except ValueError:
                    stop_gate = "CORRUPT_LEASE_TIMESTAMP"
                    proven_incident = True
            provider_state = record.last_observed_provider_state or {}
            terminal_failed = str(provider_state.get("state") or "").upper() == "FAILED"

        watchdog_lanes.append(
            {
                "lane_id": lane_id,
                "blocked": bool(stop_gate),
                "next_action": None if stop_gate else "CONTINUE_AUTHORITY_AWARE_OBSERVATION",
                "stop_gate": stop_gate,
                "auto_safe_incident_proven": proven_incident,
                "auto_safe_treated": False,
                "terminal_failed_session": terminal_failed,
            }
        )

    health = evaluate_control_cycle(watchdog_lanes)
    unresolved_operation_count = sum(len(values) for values in unresolved_by_lane.values())
    cycle_failed = health["cycle_status"] != "CONTROL_CYCLE_OK" or unresolved_operation_count > 0
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "LIVE_RUNTIME_WATCHDOG_AUDIT",
        "cycle_status": "CONTROL_CYCLE_FAILED" if cycle_failed else "CONTROL_CYCLE_OK",
        "lane_count": len(lane_ids),
        "operation_count": len(operation_keys),
        "lane_status_counts": dict(sorted(lane_status_counts.items())),
        "operation_state_counts": dict(sorted(operation_state_counts.items())),
        "unresolved_operation_count": unresolved_operation_count,
        "blocked_lanes": health["blocked_lanes"],
        "executable_lanes": health["executable_lanes"],
        "forgotten_lanes": health["forgotten_lanes"],
        "terminal_failed_sessions": health["terminal_failed_sessions"],
        "blocked_lane_freezes_independent_lanes": health[
            "blocked_lane_freezes_independent_lanes"
        ],
        "mutation_performed": False,
    }


def run_jules_read_only_probe() -> dict[str, Any]:
    api_key = _env("JULES_API_KEY")
    result = BoundedJulesProbeClient(api_key).probe_authentication()
    result["credential_source"] = "RUNTIME_SECRET"
    result["secret_value_emitted"] = False
    return result


def record_jules_probe_proof() -> dict[str, Any]:
    """Persist sanitized proof only after the preceding read-only probe succeeded."""

    repository = _env("GITHUB_REPOSITORY")
    store = build_live_state_store()
    lane_id = canonical_lane_id(PROBE_PROJECT, PROBE_ROUTE, PROBE_WORKSTREAM)
    lane = store.read_workstream(lane_id)
    if lane.status != "OK" or lane.record is None:
        raise StateUnavailable(lane.reason or "live probe lane is unavailable")

    operation_key = "ues-v2:jules-read-only-probe:" + sha256(
        f"{repository}|jules-read-only-probe-v1".encode("utf-8")
    ).hexdigest()
    current = store.read_operation(operation_key)
    now = _iso(_utc_now())
    receipt = {
        "result": "JULES_READ_ONLY_AUTHENTICATION_PASS",
        "read_only": True,
        "provider_mutation": False,
        "session_identity_persisted": False,
        "secret_material_persisted": False,
        "confirmed_at": now,
    }
    if current.status == "MISSING":
        record = OperationRecord(
            operation_key=operation_key,
            lane_id=lane_id,
            workstream_id=PROBE_WORKSTREAM,
            action="jules-read-only-authentication-probe",
            request_digest=sha256(b"JULES_READ_ONLY_AUTH_PROBE_V1").hexdigest(),
            state="CONFIRMED",
            owner="ues-live-runtime",
            started_at=now,
            updated_at=now,
            receipt=receipt,
        )
        saved = store.compare_and_swap_operation(operation_key, 0, record)
    elif current.status == "OK" and current.record is not None:
        if current.record.state != "CONFIRMED":
            raise StateUnavailable("existing Jules probe proof is not in CONFIRMED state")
        record = OperationRecord.from_dict(current.record.to_dict())
        record.updated_at = now
        record.receipt = receipt
        saved = store.compare_and_swap_operation(operation_key, current.version, record)
    else:
        raise StateUnavailable(current.reason or "Jules probe proof state is unavailable")

    return {
        "schema_version": SCHEMA_VERSION,
        "result": "JULES_READ_ONLY_PROBE_PROOF_PERSISTED",
        "operation_key": operation_key,
        "operation_state": saved.record.state if saved.record else None,
        "operation_version": saved.version,
        "provider_mutation_performed": False,
        "session_identity_persisted": False,
        "secret_material_persisted": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES live runtime foundation")
    parser.add_argument(
        "command",
        choices=("state-smoke", "state-audit", "jules-probe", "record-jules-probe"),
    )
    args = parser.parse_args(argv)
    if args.command == "state-smoke":
        result = run_state_smoke()
    elif args.command == "state-audit":
        result = run_state_audit()
    elif args.command == "record-jules-probe":
        result = record_jules_probe_proof()
    else:
        result = run_jules_read_only_probe()
    print(json.dumps(result, sort_keys=True))
    if args.command == "state-audit" and result.get("cycle_status") != "CONTROL_CYCLE_OK":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
