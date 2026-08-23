from __future__ import annotations

import json
import os
import tempfile
import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from .operation_records import sanitize_receipt

SCHEMA_VERSION = "1.0"
SHADOW_MODE = "SHADOW"
VALID_ACTIVATION_MODES = {"SHADOW", "CANARY", "ACTIVE_AUTO_SAFE"}
VALID_OPERATION_STATES = {
    "PLANNED",
    "IN_FLIGHT",
    "UNKNOWN",
    "CONFIRMED",
    "REJECTED",
    "CANCELLED",
    "RECONCILED_NOT_OBSERVED",
}


class StateStoreError(RuntimeError):
    pass


class StateUnavailable(StateStoreError):
    pass


class StateVersionConflict(StateStoreError):
    pass


class LeaseCollision(StateStoreError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Lease:
    lease_id: str
    owner: str
    operation_key: str
    acquired_at: str
    expires_at: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Lease":
        lease = cls(**{name: str(value[name]) for name in cls.__dataclass_fields__})
        _parse_time(lease.acquired_at)
        _parse_time(lease.expires_at)
        return lease


@dataclass
class WorkstreamRuntimeRecord:
    workstream_id: str
    project: str
    route: str | None = None
    activation_mode: str = SHADOW_MODE
    action_in_flight: dict[str, Any] | None = None
    lease: Lease | None = None
    operation_key: str | None = None
    operation_receipt: dict[str, Any] | None = None
    last_observed_provider_state: dict[str, Any] | None = None
    last_observed_github_state: dict[str, Any] | None = None
    last_successful_transition: dict[str, Any] | None = None
    unknown_write_state: dict[str, Any] | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if not self.workstream_id or not self.project:
            raise ValueError("workstream_id and project are required")
        if self.activation_mode not in VALID_ACTIVATION_MODES:
            self.activation_mode = SHADOW_MODE

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["operation_receipt"] = sanitize_receipt(value.get("operation_receipt"))
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkstreamRuntimeRecord":
        if not isinstance(value, dict):
            raise ValueError("workstream record must be an object")
        lease_raw = value.get("lease")
        lease = Lease.from_dict(lease_raw) if isinstance(lease_raw, dict) else None
        return cls(
            workstream_id=str(value["workstream_id"]),
            project=str(value["project"]),
            route=str(value["route"]) if value.get("route") is not None else None,
            activation_mode=str(value.get("activation_mode") or SHADOW_MODE),
            action_in_flight=(
                value.get("action_in_flight")
                if isinstance(value.get("action_in_flight"), dict)
                else None
            ),
            lease=lease,
            operation_key=(
                str(value["operation_key"]) if value.get("operation_key") else None
            ),
            operation_receipt=(
                value.get("operation_receipt")
                if isinstance(value.get("operation_receipt"), dict)
                else None
            ),
            last_observed_provider_state=(
                value.get("last_observed_provider_state")
                if isinstance(value.get("last_observed_provider_state"), dict)
                else None
            ),
            last_observed_github_state=(
                value.get("last_observed_github_state")
                if isinstance(value.get("last_observed_github_state"), dict)
                else None
            ),
            last_successful_transition=(
                value.get("last_successful_transition")
                if isinstance(value.get("last_successful_transition"), dict)
                else None
            ),
            unknown_write_state=(
                value.get("unknown_write_state")
                if isinstance(value.get("unknown_write_state"), dict)
                else None
            ),
            updated_at=str(value["updated_at"]) if value.get("updated_at") else None,
        )


@dataclass
class OperationRecord:
    operation_key: str
    workstream_id: str
    action: str
    request_digest: str
    state: str
    owner: str
    started_at: str
    updated_at: str
    attempt: int = 1
    receipt: dict[str, Any] = field(default_factory=dict)
    reconciliation_required: bool = False
    authoritative_readback: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.state not in VALID_OPERATION_STATES:
            raise ValueError(f"unsupported operation state: {self.state}")
        if not self.operation_key or not self.workstream_id or not self.request_digest:
            raise ValueError(
                "operation_key, workstream_id, and request_digest are required"
            )
        _parse_time(self.started_at)
        _parse_time(self.updated_at)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["receipt"] = sanitize_receipt(value.get("receipt"))
        value["authoritative_readback"] = sanitize_receipt(
            value.get("authoritative_readback")
        )
        value["safe_to_blind_retry"] = False
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OperationRecord":
        return cls(
            operation_key=str(value["operation_key"]),
            workstream_id=str(value["workstream_id"]),
            action=str(value.get("action") or "unknown"),
            request_digest=str(value["request_digest"]),
            state=str(value["state"]),
            owner=str(value.get("owner") or "unknown"),
            started_at=str(value["started_at"]),
            updated_at=str(value["updated_at"]),
            attempt=int(value.get("attempt") or 1),
            receipt=value.get("receipt") if isinstance(value.get("receipt"), dict) else {},
            reconciliation_required=bool(
                value.get("reconciliation_required", False)
            ),
            authoritative_readback=(
                value.get("authoritative_readback")
                if isinstance(value.get("authoritative_readback"), dict)
                else None
            ),
        )


@dataclass(frozen=True)
class StateRead:
    status: str
    version: int
    record: WorkstreamRuntimeRecord | None
    effective_activation_mode: str
    mutation_allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class OperationRead:
    status: str
    version: int
    record: OperationRecord | None
    reason: str | None = None


@dataclass(frozen=True)
class LeaseAcquireResult:
    lease: Lease
    version: int
    stale_recovered: bool


class StateStore(ABC):
    """Backend-neutral runtime-state contract; it is not project truth."""

    @abstractmethod
    def read_workstream(self, workstream_id: str) -> StateRead:
        ...

    @abstractmethod
    def compare_and_swap_workstream(
        self,
        workstream_id: str,
        expected_version: int,
        record: WorkstreamRuntimeRecord,
    ) -> StateRead:
        ...

    @abstractmethod
    def read_operation(self, operation_key: str) -> OperationRead:
        ...

    @abstractmethod
    def compare_and_swap_operation(
        self,
        operation_key: str,
        expected_version: int,
        record: OperationRecord,
    ) -> OperationRead:
        ...

    @abstractmethod
    def acquire_lease(
        self,
        workstream_id: str,
        owner: str,
        operation_key: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> LeaseAcquireResult:
        ...

    @abstractmethod
    def release_lease(
        self,
        workstream_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
    ) -> StateRead:
        ...


class DeterministicFileStateStore(StateStore):
    """Deterministic file backend for tests/replay, not a production backend choice."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = Path(path)
        self.clock = clock or _utc_now
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._lock:
            if self.path.exists():
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._write_doc(
                {
                    "schema_version": SCHEMA_VERSION,
                    "store_revision": 0,
                    "workstreams": {},
                    "operations": {},
                }
            )

    def _read_doc(self) -> dict[str, Any]:
        if not self.path.exists():
            raise StateUnavailable(
                "runtime state is missing; effective mode is SHADOW"
            )
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateUnavailable(
                "runtime state is corrupt; effective mode is SHADOW"
            ) from exc
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise StateUnavailable(
                "runtime state schema is invalid; effective mode is SHADOW"
            )
        if not isinstance(value.get("workstreams"), dict) or not isinstance(
            value.get("operations"), dict
        ):
            raise StateUnavailable(
                "runtime state collections are corrupt; effective mode is SHADOW"
            )
        return value

    def _write_doc(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _shadow_read(self, status: str, reason: str) -> StateRead:
        return StateRead(
            status=status,
            version=0,
            record=None,
            effective_activation_mode=SHADOW_MODE,
            mutation_allowed=False,
            reason=reason,
        )

    def read_workstream(self, workstream_id: str) -> StateRead:
        with self._lock:
            try:
                doc = self._read_doc()
            except StateUnavailable as exc:
                status = "MISSING" if not self.path.exists() else "CORRUPT"
                return self._shadow_read(status, str(exc))
            entry = doc["workstreams"].get(workstream_id)
            if entry is None:
                return self._shadow_read(
                    "MISSING", "workstream runtime state is missing"
                )
            try:
                version = int(entry["version"])
                record = WorkstreamRuntimeRecord.from_dict(entry["record"])
                if record.workstream_id != workstream_id:
                    raise ValueError("workstream identity mismatch")
            except (KeyError, TypeError, ValueError) as exc:
                return self._shadow_read(
                    "CORRUPT", f"workstream runtime state is corrupt: {exc}"
                )
            return StateRead(
                status="OK",
                version=version,
                record=record,
                effective_activation_mode=record.activation_mode,
                mutation_allowed=record.activation_mode != SHADOW_MODE,
            )

    def compare_and_swap_workstream(
        self,
        workstream_id: str,
        expected_version: int,
        record: WorkstreamRuntimeRecord,
    ) -> StateRead:
        if record.workstream_id != workstream_id:
            raise ValueError("workstream identity mismatch")
        with self._lock:
            doc = self._read_doc()
            entry = doc["workstreams"].get(workstream_id)
            actual_version = int(entry["version"]) if entry is not None else 0
            if actual_version != expected_version:
                raise StateVersionConflict(
                    f"workstream {workstream_id} version {actual_version} "
                    f"!= expected {expected_version}"
                )
            if entry is not None:
                try:
                    WorkstreamRuntimeRecord.from_dict(entry["record"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise StateUnavailable(
                        "cannot overwrite corrupt runtime state"
                    ) from exc
            record.updated_at = _iso(self.clock())
            new_version = actual_version + 1
            doc["workstreams"][workstream_id] = {
                "version": new_version,
                "record": record.to_dict(),
            }
            doc["store_revision"] = int(doc.get("store_revision", 0)) + 1
            self._write_doc(doc)
            return self.read_workstream(workstream_id)

    def read_operation(self, operation_key: str) -> OperationRead:
        with self._lock:
            try:
                doc = self._read_doc()
            except StateUnavailable as exc:
                status = "MISSING" if not self.path.exists() else "CORRUPT"
                return OperationRead(
                    status=status, version=0, record=None, reason=str(exc)
                )
            entry = doc["operations"].get(operation_key)
            if entry is None:
                return OperationRead(status="MISSING", version=0, record=None)
            try:
                version = int(entry["version"])
                record = OperationRecord.from_dict(entry["record"])
                if record.operation_key != operation_key:
                    raise ValueError("operation identity mismatch")
            except (KeyError, TypeError, ValueError) as exc:
                return OperationRead(
                    status="CORRUPT", version=0, record=None, reason=str(exc)
                )
            return OperationRead(status="OK", version=version, record=record)

    def compare_and_swap_operation(
        self,
        operation_key: str,
        expected_version: int,
        record: OperationRecord,
    ) -> OperationRead:
        if record.operation_key != operation_key:
            raise ValueError("operation identity mismatch")
        with self._lock:
            doc = self._read_doc()
            entry = doc["operations"].get(operation_key)
            actual_version = int(entry["version"]) if entry is not None else 0
            if actual_version != expected_version:
                raise StateVersionConflict(
                    f"operation {operation_key} version {actual_version} "
                    f"!= expected {expected_version}"
                )
            if entry is not None:
                try:
                    OperationRecord.from_dict(entry["record"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise StateUnavailable(
                        "cannot overwrite corrupt operation state"
                    ) from exc
            new_version = actual_version + 1
            doc["operations"][operation_key] = {
                "version": new_version,
                "record": record.to_dict(),
            }
            doc["store_revision"] = int(doc.get("store_revision", 0)) + 1
            self._write_doc(doc)
            return self.read_operation(operation_key)

    def acquire_lease(
        self,
        workstream_id: str,
        owner: str,
        operation_key: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> LeaseAcquireResult:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        current = (now or self.clock()).astimezone(timezone.utc)
        with self._lock:
            doc = self._read_doc()
            entry = doc["workstreams"].get(workstream_id)
            if entry is None:
                raise StateUnavailable(
                    "cannot lease missing workstream runtime state"
                )
            try:
                record = WorkstreamRuntimeRecord.from_dict(entry["record"])
                version = int(entry["version"])
            except (KeyError, TypeError, ValueError) as exc:
                raise StateUnavailable(
                    "cannot lease corrupt workstream runtime state"
                ) from exc
            stale_recovered = False
            if record.lease is not None:
                expiry = _parse_time(record.lease.expires_at)
                if current < expiry:
                    raise LeaseCollision(
                        f"workstream {workstream_id} already leased by "
                        f"{record.lease.owner}"
                    )
                stale_recovered = True
            seed = (
                f"{workstream_id}|{owner}|{operation_key}|{_iso(current)}|"
                f"{version + 1}"
            )
            lease = Lease(
                lease_id=sha256(seed.encode("utf-8")).hexdigest()[:32],
                owner=owner,
                operation_key=operation_key,
                acquired_at=_iso(current),
                expires_at=_iso(current + timedelta(seconds=ttl_seconds)),
            )
            record.lease = lease
            record.action_in_flight = {
                "operation_key": operation_key,
                "owner": owner,
                "started_at": _iso(current),
            }
            record.operation_key = operation_key
            record.updated_at = _iso(current)
            new_version = version + 1
            doc["workstreams"][workstream_id] = {
                "version": new_version,
                "record": record.to_dict(),
            }
            doc["store_revision"] = int(doc.get("store_revision", 0)) + 1
            self._write_doc(doc)
            return LeaseAcquireResult(
                lease=lease,
                version=new_version,
                stale_recovered=stale_recovered,
            )

    def release_lease(
        self,
        workstream_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
    ) -> StateRead:
        current = (now or self.clock()).astimezone(timezone.utc)
        with self._lock:
            read = self.read_workstream(workstream_id)
            if read.status != "OK" or read.record is None:
                raise StateUnavailable(
                    read.reason or "workstream runtime state unavailable"
                )
            record = read.record
            if record.lease is None:
                return read
            if record.lease.lease_id != lease_id:
                raise LeaseCollision("lease ownership mismatch")
            record.lease = None
            record.action_in_flight = None
            record.updated_at = _iso(current)
            return self.compare_and_swap_workstream(
                workstream_id, read.version, record
            )


def claim_operation(
    store: StateStore,
    *,
    workstream_id: str,
    owner: str,
    operation_key: str,
    action: str,
    request_digest: str,
    ttl_seconds: int,
    receipt: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim one logical mutation using an idempotency record and workstream lease."""
    from .idempotency import evaluate_idempotency

    ws = store.read_workstream(workstream_id)
    if ws.status != "OK" or ws.record is None or not ws.mutation_allowed:
        return {
            "decision": (
                "SHADOW_STATE_UNAVAILABLE" if ws.status != "OK" else "SHADOW_MODE"
            ),
            "mutation_allowed": False,
            "effective_activation_mode": ws.effective_activation_mode,
            "safe_to_blind_retry": False,
            "reason": ws.reason,
        }

    existing = store.read_operation(operation_key)
    records = (
        [existing.record.to_dict()]
        if existing.status == "OK" and existing.record
        else []
    )
    idem = evaluate_idempotency(operation_key, request_digest, records)
    if not idem["safe_to_execute"]:
        return {
            "decision": idem["decision"],
            "mutation_allowed": False,
            "safe_to_blind_retry": False,
            "idempotency": idem,
        }
    if existing.status == "CORRUPT":
        return {
            "decision": "CORRUPT_OPERATION_STATE",
            "mutation_allowed": False,
            "safe_to_blind_retry": False,
            "reason": existing.reason,
        }

    try:
        lease_result = store.acquire_lease(
            workstream_id,
            owner,
            operation_key,
            ttl_seconds,
            now=now,
        )
    except (LeaseCollision, StateUnavailable) as exc:
        return {
            "decision": "LEASE_CONFLICT_OR_STATE_UNAVAILABLE",
            "mutation_allowed": False,
            "safe_to_blind_retry": False,
            "reason": str(exc),
        }

    current = (now or _utc_now()).astimezone(timezone.utc)
    try:
        if existing.status == "OK" and existing.record is not None:
            record = existing.record
            record.state = "IN_FLIGHT"
            record.owner = owner
            record.updated_at = _iso(current)
            record.attempt += 1
            record.reconciliation_required = False
            record.receipt = sanitize_receipt(receipt or record.receipt)
            expected_operation_version = existing.version
        else:
            record = OperationRecord(
                operation_key=operation_key,
                workstream_id=workstream_id,
                action=action,
                request_digest=request_digest,
                state="IN_FLIGHT",
                owner=owner,
                started_at=_iso(current),
                updated_at=_iso(current),
                receipt=sanitize_receipt(receipt or {}),
            )
            expected_operation_version = 0
        saved = store.compare_and_swap_operation(
            operation_key, expected_operation_version, record
        )
    except (StateVersionConflict, StateUnavailable):
        try:
            store.release_lease(
                workstream_id, lease_result.lease.lease_id, now=current
            )
        except StateStoreError:
            pass
        return {
            "decision": "OPERATION_CLAIM_RACE",
            "mutation_allowed": False,
            "safe_to_blind_retry": False,
        }

    ws_after = store.read_workstream(workstream_id)
    if ws_after.status == "OK" and ws_after.record is not None:
        ws_record = ws_after.record
        ws_record.operation_key = operation_key
        ws_record.operation_receipt = sanitize_receipt(receipt or {})
        try:
            store.compare_and_swap_workstream(
                workstream_id, ws_after.version, ws_record
            )
        except StateStoreError:
            return {
                "decision": "CLAIMED_RECONCILE_WORKSTREAM_REQUIRED",
                "mutation_allowed": False,
                "safe_to_blind_retry": False,
                "operation_version": saved.version,
                "lease_id": lease_result.lease.lease_id,
            }

    return {
        "decision": "CLAIMED",
        "mutation_allowed": True,
        "safe_to_blind_retry": False,
        "operation_version": saved.version,
        "lease_id": lease_result.lease.lease_id,
        "stale_lease_recovered": lease_result.stale_recovered,
    }


def record_unknown_write(
    store: StateStore,
    *,
    workstream_id: str,
    operation_key: str,
    result: dict[str, Any],
    now: datetime | None = None,
) -> OperationRead:
    current = (now or _utc_now()).astimezone(timezone.utc)
    operation = store.read_operation(operation_key)
    if operation.status != "OK" or operation.record is None:
        raise StateUnavailable(
            "cannot record UNKNOWN for missing/corrupt operation"
        )
    record = operation.record
    record.state = "UNKNOWN"
    record.updated_at = _iso(current)
    record.reconciliation_required = True
    record.receipt = sanitize_receipt(
        {**record.receipt, "result": result, "state": "UNKNOWN"}
    )
    saved = store.compare_and_swap_operation(
        operation_key, operation.version, record
    )

    ws = store.read_workstream(workstream_id)
    if ws.status != "OK" or ws.record is None:
        raise StateUnavailable(
            "operation is UNKNOWN but workstream state is unavailable"
        )
    ws_record = ws.record
    ws_record.unknown_write_state = {
        "operation_key": operation_key,
        "recorded_at": _iso(current),
        "reconciliation_required": True,
    }
    ws_record.operation_receipt = record.receipt
    store.compare_and_swap_workstream(workstream_id, ws.version, ws_record)
    return saved


def record_authoritative_readback(
    store: StateStore,
    *,
    workstream_id: str,
    operation_key: str,
    observed: bool | None,
    evidence: dict[str, Any],
    now: datetime | None = None,
) -> OperationRead:
    """Resolve UNKNOWN from authoritative readback before any retry.

    ``observed=True`` confirms the write. ``False`` proves it was not observed
    and unlocks a deliberate retry of the same logical operation key. ``None``
    remains UNKNOWN and blocks retries.
    """
    current = (now or _utc_now()).astimezone(timezone.utc)
    operation = store.read_operation(operation_key)
    if operation.status != "OK" or operation.record is None:
        raise StateUnavailable("operation state unavailable for readback")
    record = operation.record
    if record.state != "UNKNOWN":
        raise StateStoreError(
            "authoritative readback is only valid for UNKNOWN operations"
        )
    safe_evidence = sanitize_receipt(evidence)
    record.authoritative_readback = {
        "observed": observed,
        "evidence": safe_evidence,
        "read_at": _iso(current),
    }
    record.updated_at = _iso(current)
    if observed is True:
        record.state = "CONFIRMED"
        record.reconciliation_required = False
    elif observed is False:
        record.state = "RECONCILED_NOT_OBSERVED"
        record.reconciliation_required = False
    else:
        record.state = "UNKNOWN"
        record.reconciliation_required = True
    record.receipt = sanitize_receipt(
        {
            **record.receipt,
            "state": record.state,
            "post_condition": record.authoritative_readback,
        }
    )
    saved = store.compare_and_swap_operation(
        operation_key, operation.version, record
    )

    ws = store.read_workstream(workstream_id)
    if ws.status != "OK" or ws.record is None:
        raise StateUnavailable(
            "operation readback saved but workstream state unavailable"
        )
    ws_record = ws.record
    ws_record.operation_receipt = record.receipt
    if observed is None:
        ws_record.unknown_write_state = {
            "operation_key": operation_key,
            "recorded_at": _iso(current),
            "reconciliation_required": True,
        }
    else:
        ws_record.unknown_write_state = None
        if ws_record.lease and ws_record.lease.operation_key == operation_key:
            ws_record.lease = None
            ws_record.action_in_flight = None
    store.compare_and_swap_workstream(workstream_id, ws.version, ws_record)
    return saved
