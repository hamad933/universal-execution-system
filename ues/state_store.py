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
from typing import Any, Callable, Mapping

from .idempotency import EffectIdentity, effect_operation_key
from .operation_records import sanitize_receipt

SCHEMA_VERSION = "1.2"
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

STATE_SERIALIZATION_REQUIREMENTS = {
    "schema_version": SCHEMA_VERSION,
    "encoding": "utf-8",
    "format": "json",
    "canonicalization": "sorted keys; deterministic scalar representation",
    "versioning": "monotonic per-record version; backend CAS token may be stronger",
    "atomicity": "compare-and-swap must be atomic across concurrent runners",
    "durability": "successful writes must be visible after runner replacement",
    "lease_scope": "canonical lane_id local; never global portfolio lock",
    "operation_order": "durable IN_FLIGHT record before provider mutation",
    "restart_rule": "IN_FLIGHT/UNKNOWN requires authoritative readback before retry",
    "conflict_rule": "version/CAS conflict fails closed without overwrite",
    "secret_rule": "persist sanitized receipts/evidence/bindings only; never raw secrets",
    "lane_rule": "runtime keys use Integration-owned lane_id derived from project/route/workstream",
}
PRODUCTION_CAPABILITY_FIELDS = (
    "survives_runner_replacement",
    "atomic_compare_and_swap",
    "versioned_state",
    "lane_local_leases",
    "durable_operation_records",
    "authoritative_restart_reconciliation",
    "conflict_detection",
)


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


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _exact(value: Mapping[str, Any] | None) -> dict[str, str]:
    if value is None:
        return {}
    return {
        str(k): str(v)
        for k, v in sorted(value.items(), key=lambda x: str(x[0]))
        if v is not None
    }


def _sanitize_actor_bindings(value: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for role, binding in value.items():
        if not isinstance(binding, Mapping):
            raise ValueError("actor binding values must be objects")
        role_name = _required(role, "actor role").upper()
        result[role_name] = sanitize_receipt(dict(binding))
    return result


@dataclass(frozen=True)
class StateStoreCapabilities:
    backend_name: str
    survives_runner_replacement: bool
    atomic_compare_and_swap: bool
    versioned_state: bool
    lane_local_leases: bool
    durable_operation_records: bool
    authoritative_restart_reconciliation: bool
    conflict_detection: bool


def production_state_store_assessment(
    store_or_capabilities: "StateStore | StateStoreCapabilities",
) -> dict[str, Any]:
    caps = (
        store_or_capabilities.capabilities
        if isinstance(store_or_capabilities, StateStore)
        else store_or_capabilities
    )
    missing = [
        name for name in PRODUCTION_CAPABILITY_FIELDS if not bool(getattr(caps, name))
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "backend_name": caps.backend_name,
        "ready_for_cross_run_production": not missing,
        "missing_capabilities": missing,
        "serialization_requirements": dict(STATE_SERIALIZATION_REQUIREMENTS),
    }


@dataclass(frozen=True)
class Lease:
    lease_id: str
    owner: str
    operation_key: str
    acquired_at: str
    expires_at: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Lease":
        lease = cls(**{name: str(value[name]) for name in cls.__dataclass_fields__})
        _parse_time(lease.acquired_at)
        _parse_time(lease.expires_at)
        return lease


@dataclass
class CanaryGrant:
    """Exact bounded mutation capability; CANARY mode alone grants nothing."""

    authority_event_id: str
    lane_id: str
    project: str
    route: str
    workstream_id: str
    effect_type: str
    target: dict[str, str]
    issued_at: str
    expires_at: str
    maximum_effect_count: int = 1
    expected_start: dict[str, str] | None = None
    consumed_count: int = 0
    consumed_at: str | None = None
    consumed_operation_keys: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in (
            "authority_event_id",
            "lane_id",
            "project",
            "route",
            "workstream_id",
            "effect_type",
        ):
            _required(getattr(self, name), name)
        self.target = _exact(self.target)
        if not self.target:
            raise ValueError("canary exact target is required")
        self.expected_start = (
            _exact(self.expected_start) if self.expected_start is not None else None
        )
        issued, expiry = _parse_time(self.issued_at), _parse_time(self.expires_at)
        if expiry <= issued:
            raise ValueError("canary expires_at must be after issued_at")
        if self.maximum_effect_count <= 0 or not 0 <= self.consumed_count <= self.maximum_effect_count:
            raise ValueError("invalid canary effect count")
        if self.consumed_at:
            _parse_time(self.consumed_at)

    @property
    def consumed(self) -> bool:
        return self.consumed_count >= self.maximum_effect_count

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["consumed"] = self.consumed
        return sanitize_receipt(value)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CanaryGrant":
        target = value.get("target")
        expected = value.get("expected_start")
        keys = value.get("consumed_operation_keys") or []
        if not isinstance(target, Mapping) or (
            expected is not None and not isinstance(expected, Mapping)
        ) or not isinstance(keys, list):
            raise ValueError("invalid canary grant structure")
        return cls(
            authority_event_id=str(value.get("authority_event_id") or ""),
            lane_id=str(value.get("lane_id") or ""),
            project=str(value.get("project") or ""),
            route=str(value.get("route") or ""),
            workstream_id=str(value.get("workstream_id") or ""),
            effect_type=str(value.get("effect_type") or ""),
            target=_exact(target),
            issued_at=str(value.get("issued_at") or ""),
            expires_at=str(value.get("expires_at") or ""),
            maximum_effect_count=int(value.get("maximum_effect_count") or 1),
            expected_start=_exact(expected) if isinstance(expected, Mapping) else None,
            consumed_count=int(value.get("consumed_count") or 0),
            consumed_at=str(value["consumed_at"]) if value.get("consumed_at") else None,
            consumed_operation_keys=[str(x) for x in keys],
        )


@dataclass(frozen=True)
class MutationAuthorization:
    effect_identity: EffectIdentity
    authority_event_id: str
    project_policy_authorized: bool
    exact_binding_proven: bool
    evidence_verified: bool
    expires_at: str | None = None


@dataclass
class WorkstreamRuntimeRecord:
    lane_id: str
    project: str
    route: str
    workstream_id: str
    activation_mode: str = SHADOW_MODE
    actor_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    authority_provenance: dict[str, Any] | None = None
    evidence_bindings: dict[str, Any] | None = None
    action_in_flight: dict[str, Any] | None = None
    lease: Lease | None = None
    operation_key: str | None = None
    operation_receipt: dict[str, Any] | None = None
    last_observed_provider_state: dict[str, Any] | None = None
    last_observed_github_state: dict[str, Any] | None = None
    last_successful_transition: dict[str, Any] | None = None
    unknown_write_state: dict[str, Any] | None = None
    canary_grants: list[CanaryGrant] = field(default_factory=list)
    updated_at: str | None = None

    def __post_init__(self) -> None:
        for name in ("lane_id", "project", "route", "workstream_id"):
            _required(getattr(self, name), name)
        self.actor_bindings = _sanitize_actor_bindings(self.actor_bindings)
        if self.activation_mode not in VALID_ACTIVATION_MODES:
            self.activation_mode = SHADOW_MODE

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["actor_bindings"] = _sanitize_actor_bindings(self.actor_bindings)
        value["authority_provenance"] = sanitize_receipt(value.get("authority_provenance"))
        value["evidence_bindings"] = sanitize_receipt(value.get("evidence_bindings"))
        value["operation_receipt"] = sanitize_receipt(value.get("operation_receipt"))
        value["canary_grants"] = [grant.to_dict() for grant in self.canary_grants]
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkstreamRuntimeRecord":
        lease_raw = value.get("lease")
        grants_raw = value.get("canary_grants") or []
        actors_raw = value.get("actor_bindings") or {}
        if not isinstance(grants_raw, list) or not isinstance(actors_raw, Mapping):
            raise ValueError("invalid runtime record collection")
        return cls(
            lane_id=str(value.get("lane_id") or ""),
            project=str(value.get("project") or ""),
            route=str(value.get("route") or ""),
            workstream_id=str(value.get("workstream_id") or ""),
            activation_mode=str(value.get("activation_mode") or SHADOW_MODE),
            actor_bindings=_sanitize_actor_bindings(actors_raw),
            authority_provenance=value.get("authority_provenance") if isinstance(value.get("authority_provenance"), dict) else None,
            evidence_bindings=value.get("evidence_bindings") if isinstance(value.get("evidence_bindings"), dict) else None,
            action_in_flight=value.get("action_in_flight") if isinstance(value.get("action_in_flight"), dict) else None,
            lease=Lease.from_dict(lease_raw) if isinstance(lease_raw, Mapping) else None,
            operation_key=str(value["operation_key"]) if value.get("operation_key") else None,
            operation_receipt=value.get("operation_receipt") if isinstance(value.get("operation_receipt"), dict) else None,
            last_observed_provider_state=value.get("last_observed_provider_state") if isinstance(value.get("last_observed_provider_state"), dict) else None,
            last_observed_github_state=value.get("last_observed_github_state") if isinstance(value.get("last_observed_github_state"), dict) else None,
            last_successful_transition=value.get("last_successful_transition") if isinstance(value.get("last_successful_transition"), dict) else None,
            unknown_write_state=value.get("unknown_write_state") if isinstance(value.get("unknown_write_state"), dict) else None,
            canary_grants=[CanaryGrant.from_dict(x) for x in grants_raw if isinstance(x, Mapping)],
            updated_at=str(value["updated_at"]) if value.get("updated_at") else None,
        )


@dataclass
class OperationRecord:
    operation_key: str
    lane_id: str
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
    effect_identity: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.state not in VALID_OPERATION_STATES:
            raise ValueError(f"unsupported operation state: {self.state}")
        for name in ("operation_key", "lane_id", "workstream_id", "request_digest"):
            _required(getattr(self, name), name)
        _parse_time(self.started_at)
        _parse_time(self.updated_at)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["receipt"] = sanitize_receipt(value.get("receipt"))
        value["authoritative_readback"] = sanitize_receipt(value.get("authoritative_readback"))
        value["effect_identity"] = sanitize_receipt(value.get("effect_identity"))
        value["safe_to_blind_retry"] = False
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OperationRecord":
        return cls(
            operation_key=str(value.get("operation_key") or ""),
            lane_id=str(value.get("lane_id") or ""),
            workstream_id=str(value.get("workstream_id") or ""),
            action=str(value.get("action") or "unknown"),
            request_digest=str(value.get("request_digest") or ""),
            state=str(value.get("state") or "UNKNOWN"),
            owner=str(value.get("owner") or "unknown"),
            started_at=str(value.get("started_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
            attempt=int(value.get("attempt") or 1),
            receipt=value.get("receipt") if isinstance(value.get("receipt"), dict) else {},
            reconciliation_required=bool(value.get("reconciliation_required", False)),
            authoritative_readback=value.get("authoritative_readback") if isinstance(value.get("authoritative_readback"), dict) else None,
            effect_identity=value.get("effect_identity") if isinstance(value.get("effect_identity"), dict) else None,
        )


@dataclass(frozen=True)
class StateRead:
    status: str
    version: int
    record: WorkstreamRuntimeRecord | None
    effective_activation_mode: str
    mutation_allowed: bool
    reason: str | None = None
    mode_allows_mutation_candidate: bool = False


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
    """Backend-neutral lane state; production backend selection is not made here."""

    @property
    @abstractmethod
    def capabilities(self) -> StateStoreCapabilities: ...

    @abstractmethod
    def read_workstream(self, lane_id: str) -> StateRead: ...

    @abstractmethod
    def compare_and_swap_workstream(
        self,
        lane_id: str,
        expected_version: int,
        record: WorkstreamRuntimeRecord,
    ) -> StateRead: ...

    @abstractmethod
    def read_operation(self, operation_key: str) -> OperationRead: ...

    @abstractmethod
    def compare_and_swap_operation(
        self,
        operation_key: str,
        expected_version: int,
        record: OperationRecord,
    ) -> OperationRead: ...

    @abstractmethod
    def acquire_lease(
        self,
        lane_id: str,
        owner: str,
        operation_key: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> LeaseAcquireResult: ...

    @abstractmethod
    def release_lease(
        self,
        lane_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
    ) -> StateRead: ...


class DeterministicFileStateStore(StateStore):
    """Deterministic local-file test backend; never cross-run production state."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = Path(path)
        self.clock = clock or _utc_now
        self._lock = threading.RLock()

    @property
    def capabilities(self) -> StateStoreCapabilities:
        return StateStoreCapabilities(
            backend_name="deterministic-local-file-test-only",
            survives_runner_replacement=False,
            atomic_compare_and_swap=True,
            versioned_state=True,
            lane_local_leases=True,
            durable_operation_records=False,
            authoritative_restart_reconciliation=True,
            conflict_detection=True,
        )

    def initialize(self) -> None:
        with self._lock:
            if not self.path.exists():
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
            raise StateUnavailable("runtime state is missing; effective mode is SHADOW")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateUnavailable("runtime state is corrupt; effective mode is SHADOW") from exc
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise StateUnavailable("runtime state schema is invalid; effective mode is SHADOW")
        if not isinstance(value.get("workstreams"), dict) or not isinstance(value.get("operations"), dict):
            raise StateUnavailable("runtime state collections are corrupt; effective mode is SHADOW")
        return value

    def _write_doc(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _shadow(status: str, reason: str) -> StateRead:
        return StateRead(status, 0, None, SHADOW_MODE, False, reason, False)

    def read_workstream(self, lane_id: str) -> StateRead:
        lane_id = _required(lane_id, "lane_id")
        with self._lock:
            try:
                doc = self._read_doc()
            except StateUnavailable as exc:
                return self._shadow("MISSING" if not self.path.exists() else "CORRUPT", str(exc))
            entry = doc["workstreams"].get(lane_id)
            if entry is None:
                return self._shadow("MISSING", "lane runtime state is missing")
            try:
                version = int(entry["version"])
                record = WorkstreamRuntimeRecord.from_dict(entry["record"])
                if record.lane_id != lane_id:
                    raise ValueError("lane identity mismatch")
            except (KeyError, TypeError, ValueError) as exc:
                return self._shadow("CORRUPT", f"lane runtime state is corrupt: {exc}")
            candidate = record.activation_mode in {"CANARY", "ACTIVE_AUTO_SAFE"}
            return StateRead("OK", version, record, record.activation_mode, False, None, candidate)

    def compare_and_swap_workstream(
        self,
        lane_id: str,
        expected_version: int,
        record: WorkstreamRuntimeRecord,
    ) -> StateRead:
        lane_id = _required(lane_id, "lane_id")
        if record.lane_id != lane_id:
            raise ValueError("lane identity mismatch")
        with self._lock:
            doc = self._read_doc()
            entry = doc["workstreams"].get(lane_id)
            actual = int(entry["version"]) if entry is not None else 0
            if actual != expected_version:
                raise StateVersionConflict(
                    f"lane {lane_id} version {actual} != expected {expected_version}"
                )
            if entry is not None:
                try:
                    WorkstreamRuntimeRecord.from_dict(entry["record"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise StateUnavailable("cannot overwrite corrupt runtime state") from exc
            record.updated_at = _iso(self.clock())
            new_version = actual + 1
            doc["workstreams"][lane_id] = {
                "version": new_version,
                "record": record.to_dict(),
            }
            doc["store_revision"] = int(doc.get("store_revision", 0)) + 1
            self._write_doc(doc)
            return self.read_workstream(lane_id)

    def read_operation(self, operation_key: str) -> OperationRead:
        with self._lock:
            try:
                doc = self._read_doc()
            except StateUnavailable as exc:
                return OperationRead(
                    "MISSING" if not self.path.exists() else "CORRUPT",
                    0,
                    None,
                    str(exc),
                )
            entry = doc["operations"].get(operation_key)
            if entry is None:
                return OperationRead("MISSING", 0, None)
            try:
                version = int(entry["version"])
                record = OperationRecord.from_dict(entry["record"])
                if record.operation_key != operation_key:
                    raise ValueError("operation identity mismatch")
            except (KeyError, TypeError, ValueError) as exc:
                return OperationRead("CORRUPT", 0, None, str(exc))
            return OperationRead("OK", version, record)

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
            actual = int(entry["version"]) if entry is not None else 0
            if actual != expected_version:
                raise StateVersionConflict(
                    f"operation {operation_key} version {actual} != expected {expected_version}"
                )
            if entry is not None:
                try:
                    OperationRecord.from_dict(entry["record"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise StateUnavailable("cannot overwrite corrupt operation state") from exc
            new_version = actual + 1
            doc["operations"][operation_key] = {
                "version": new_version,
                "record": record.to_dict(),
            }
            doc["store_revision"] = int(doc.get("store_revision", 0)) + 1
            self._write_doc(doc)
            return self.read_operation(operation_key)

    def acquire_lease(
        self,
        lane_id: str,
        owner: str,
        operation_key: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> LeaseAcquireResult:
        lane_id = _required(lane_id, "lane_id")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        current = (now or self.clock()).astimezone(timezone.utc)
        with self._lock:
            doc = self._read_doc()
            entry = doc["workstreams"].get(lane_id)
            if entry is None:
                raise StateUnavailable("cannot lease missing lane runtime state")
            try:
                record = WorkstreamRuntimeRecord.from_dict(entry["record"])
                version = int(entry["version"])
                if record.lane_id != lane_id:
                    raise ValueError("lane identity mismatch")
            except (KeyError, TypeError, ValueError) as exc:
                raise StateUnavailable("cannot lease corrupt lane runtime state") from exc
            stale = False
            if record.lease is not None:
                if current < _parse_time(record.lease.expires_at):
                    raise LeaseCollision(
                        f"lane {lane_id} already leased by {record.lease.owner}"
                    )
                stale = True
            seed = f"{lane_id}|{owner}|{operation_key}|{_iso(current)}|{version + 1}"
            lease = Lease(
                sha256(seed.encode()).hexdigest()[:32],
                owner,
                operation_key,
                _iso(current),
                _iso(current + timedelta(seconds=ttl_seconds)),
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
            doc["workstreams"][lane_id] = {
                "version": new_version,
                "record": record.to_dict(),
            }
            doc["store_revision"] = int(doc.get("store_revision", 0)) + 1
            self._write_doc(doc)
            return LeaseAcquireResult(lease, new_version, stale)

    def release_lease(
        self,
        lane_id: str,
        lease_id: str,
        *,
        now: datetime | None = None,
    ) -> StateRead:
        read = self.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or "lane runtime state unavailable")
        record = read.record
        if record.lease is None:
            return read
        if record.lease.lease_id != lease_id:
            raise LeaseCollision("lease ownership mismatch")
        record.lease = None
        record.action_in_flight = None
        record.updated_at = _iso((now or self.clock()).astimezone(timezone.utc))
        return self.compare_and_swap_workstream(lane_id, read.version, record)


def _same_effect(left: EffectIdentity, right: EffectIdentity) -> bool:
    return left == right


def _effect_matches_record(record: WorkstreamRuntimeRecord, effect: EffectIdentity) -> bool:
    return (
        effect.lane_id == record.lane_id
        and effect.project == record.project
        and effect.route == record.route
        and effect.workstream_id == record.workstream_id
    )


def evaluate_canary_grant(
    record: WorkstreamRuntimeRecord,
    effect: EffectIdentity,
    *,
    observed_start: Mapping[str, Any] | None = None,
    authority_event_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not _effect_matches_record(record, effect):
        return {"allowed": False, "decision": "CANARY_LANE_MISMATCH"}
    current = (now or _utc_now()).astimezone(timezone.utc)
    exact_target = dict(effect.target)
    candidates = [
        grant
        for grant in record.canary_grants
        if grant.lane_id == effect.lane_id
        and grant.project == effect.project
        and grant.route == effect.route
        and grant.workstream_id == effect.workstream_id
        and grant.effect_type == effect.action
        and grant.target == exact_target
        and (authority_event_id is None or grant.authority_event_id == authority_event_id)
    ]
    if not candidates:
        return {"allowed": False, "decision": "CANARY_GRANT_NOT_FOUND"}
    if len(candidates) != 1:
        return {"allowed": False, "decision": "CANARY_GRANT_AMBIGUOUS"}
    grant = candidates[0]
    if current < _parse_time(grant.issued_at):
        return {"allowed": False, "decision": "CANARY_GRANT_NOT_YET_VALID"}
    if current >= _parse_time(grant.expires_at):
        return {"allowed": False, "decision": "CANARY_GRANT_EXPIRED"}
    if grant.consumed:
        return {"allowed": False, "decision": "CANARY_GRANT_CONSUMED"}
    observed = _exact(observed_start)
    if grant.expected_start and any(
        observed.get(k) != v for k, v in grant.expected_start.items()
    ):
        return {"allowed": False, "decision": "CANARY_EXPECTED_START_MISMATCH"}
    return {
        "allowed": True,
        "decision": "CANARY_GRANT_MATCH",
        "authority_event_id": grant.authority_event_id,
    }


def evaluate_active_auto_safe_authority(
    record: WorkstreamRuntimeRecord,
    effect: EffectIdentity,
    authorization: MutationAuthorization | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if authorization is None:
        return {"allowed": False, "decision": "ACTION_AUTHORITY_REQUIRED"}
    if not _same_effect(effect, authorization.effect_identity):
        return {"allowed": False, "decision": "ACTION_AUTHORITY_EFFECT_MISMATCH"}
    if not _effect_matches_record(record, effect):
        return {"allowed": False, "decision": "ACTION_AUTHORITY_LANE_MISMATCH"}
    if not authorization.project_policy_authorized:
        return {"allowed": False, "decision": "PROJECT_POLICY_DENIED"}
    if not authorization.exact_binding_proven:
        return {"allowed": False, "decision": "EXACT_BINDING_REQUIRED"}
    if not authorization.evidence_verified:
        return {"allowed": False, "decision": "EVIDENCE_REQUIRED"}
    if authorization.expires_at and (
        (now or _utc_now()).astimezone(timezone.utc)
        >= _parse_time(authorization.expires_at)
    ):
        return {"allowed": False, "decision": "ACTION_AUTHORITY_EXPIRED"}
    return {
        "allowed": True,
        "decision": "ACTIVE_AUTO_SAFE_AUTHORIZED",
        "authority_event_id": authorization.authority_event_id,
    }


def _consume_canary(
    store: StateStore,
    lane_id: str,
    lease_id: str,
    effect: EffectIdentity,
    operation_key: str,
    authority_event_id: str,
    *,
    now: datetime,
) -> None:
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable("lane unavailable while consuming canary grant")
    record = read.record
    if record.lease is None or record.lease.lease_id != lease_id:
        raise LeaseCollision("canary grant consumption requires owned lease")
    matched = [
        grant
        for grant in record.canary_grants
        if grant.authority_event_id == authority_event_id
        and grant.lane_id == effect.lane_id
        and grant.project == effect.project
        and grant.route == effect.route
        and grant.workstream_id == effect.workstream_id
        and grant.effect_type == effect.action
        and grant.target == dict(effect.target)
    ]
    if len(matched) != 1 or matched[0].consumed:
        raise StateStoreError("canary grant unavailable during consumption")
    grant = matched[0]
    grant.consumed_count += 1
    grant.consumed_at = _iso(now)
    grant.consumed_operation_keys.append(operation_key)
    store.compare_and_swap_workstream(lane_id, read.version, record)


def claim_operation(
    store: StateStore,
    *,
    lane_id: str,
    owner: str,
    operation_key: str,
    action: str,
    request_digest: str,
    ttl_seconds: int,
    receipt: dict[str, Any] | None = None,
    effect_identity: EffectIdentity | None = None,
    authorization: MutationAuthorization | None = None,
    observed_start: Mapping[str, Any] | None = None,
    canary_authority_event_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fail closed: lane/effect/idempotency/authority -> lease -> durable IN_FLIGHT."""
    from .idempotency import evaluate_idempotency

    ws = store.read_workstream(lane_id)
    if ws.status != "OK" or ws.record is None:
        return {
            "decision": "SHADOW_STATE_UNAVAILABLE",
            "mutation_allowed": False,
            "effective_activation_mode": ws.effective_activation_mode,
            "safe_to_blind_retry": False,
            "reason": ws.reason,
        }
    record = ws.record
    if record.activation_mode == SHADOW_MODE:
        return {
            "decision": "SHADOW_MODE",
            "mutation_allowed": False,
            "effective_activation_mode": SHADOW_MODE,
            "safe_to_blind_retry": False,
        }
    if effect_identity is None:
        return {"decision": "EFFECT_IDENTITY_REQUIRED", "mutation_allowed": False, "safe_to_blind_retry": False}
    if not _effect_matches_record(record, effect_identity) or effect_identity.lane_id != lane_id:
        return {"decision": "EFFECT_IDENTITY_LANE_MISMATCH", "mutation_allowed": False, "safe_to_blind_retry": False}
    if action != effect_identity.action or operation_key != effect_operation_key(effect_identity):
        return {"decision": "EFFECT_IDENTITY_MISMATCH", "mutation_allowed": False, "safe_to_blind_retry": False}

    existing = store.read_operation(operation_key)
    if existing.status == "CORRUPT":
        return {"decision": "CORRUPT_OPERATION_STATE", "mutation_allowed": False, "safe_to_blind_retry": False, "reason": existing.reason}
    records = [existing.record.to_dict()] if existing.status == "OK" and existing.record else []
    idem = evaluate_idempotency(operation_key, request_digest, records)
    if not idem["safe_to_execute"]:
        return {"decision": idem["decision"], "mutation_allowed": False, "safe_to_blind_retry": False, "idempotency": idem}

    current = (now or _utc_now()).astimezone(timezone.utc)
    if record.activation_mode == "CANARY":
        authority = evaluate_canary_grant(
            record,
            effect_identity,
            observed_start=observed_start,
            authority_event_id=canary_authority_event_id,
            now=current,
        )
    elif record.activation_mode == "ACTIVE_AUTO_SAFE":
        authority = evaluate_active_auto_safe_authority(
            record,
            effect_identity,
            authorization,
            now=current,
        )
    else:
        authority = {"allowed": False, "decision": "SHADOW_MODE"}
    if not authority["allowed"]:
        return {"decision": authority["decision"], "mutation_allowed": False, "safe_to_blind_retry": False}

    try:
        lease = store.acquire_lease(lane_id, owner, operation_key, ttl_seconds, now=current)
    except (LeaseCollision, StateUnavailable) as exc:
        return {"decision": "LEASE_CONFLICT_OR_STATE_UNAVAILABLE", "mutation_allowed": False, "safe_to_blind_retry": False, "reason": str(exc)}

    if record.activation_mode == "CANARY":
        try:
            _consume_canary(
                store,
                lane_id,
                lease.lease.lease_id,
                effect_identity,
                operation_key,
                str(authority["authority_event_id"]),
                now=current,
            )
        except StateStoreError as exc:
            try:
                store.release_lease(lane_id, lease.lease.lease_id, now=current)
            except StateStoreError:
                pass
            return {"decision": "CANARY_GRANT_CONSUMPTION_FAILED", "mutation_allowed": False, "safe_to_blind_retry": False, "reason": str(exc)}

    safe_receipt = sanitize_receipt(
        {
            **(receipt or {}),
            "operation_key": operation_key,
            "lane_id": lane_id,
            "effect_identity": effect_identity.to_dict(),
            "request_digest": request_digest,
            "state": "IN_FLIGHT",
            "authority_event_id": authority.get("authority_event_id"),
        }
    )
    try:
        if existing.status == "OK" and existing.record is not None:
            op = existing.record
            op.state = "IN_FLIGHT"
            op.owner = owner
            op.updated_at = _iso(current)
            op.attempt += 1
            op.reconciliation_required = False
            op.receipt = safe_receipt
            op.effect_identity = effect_identity.to_dict()
            expected = existing.version
        else:
            op = OperationRecord(
                operation_key=operation_key,
                lane_id=lane_id,
                workstream_id=record.workstream_id,
                action=action,
                request_digest=request_digest,
                state="IN_FLIGHT",
                owner=owner,
                started_at=_iso(current),
                updated_at=_iso(current),
                receipt=safe_receipt,
                effect_identity=effect_identity.to_dict(),
            )
            expected = 0
        saved = store.compare_and_swap_operation(operation_key, expected, op)
    except (StateVersionConflict, StateUnavailable):
        try:
            store.release_lease(lane_id, lease.lease.lease_id, now=current)
        except StateStoreError:
            pass
        return {"decision": "OPERATION_CLAIM_RACE", "mutation_allowed": False, "safe_to_blind_retry": False}

    ws_after = store.read_workstream(lane_id)
    if ws_after.status == "OK" and ws_after.record is not None:
        runtime = ws_after.record
        runtime.operation_key = operation_key
        runtime.operation_receipt = safe_receipt
        try:
            store.compare_and_swap_workstream(lane_id, ws_after.version, runtime)
        except StateStoreError:
            return {
                "decision": "CLAIMED_RECONCILE_WORKSTREAM_REQUIRED",
                "mutation_allowed": False,
                "safe_to_blind_retry": False,
                "operation_version": saved.version,
                "lease_id": lease.lease.lease_id,
            }
    return {
        "decision": "CLAIMED",
        "mutation_allowed": True,
        "safe_to_blind_retry": False,
        "operation_version": saved.version,
        "lease_id": lease.lease.lease_id,
        "stale_lease_recovered": lease.stale_recovered,
        "authority_event_id": authority.get("authority_event_id"),
    }


def record_unknown_write(
    store: StateStore,
    *,
    lane_id: str,
    operation_key: str,
    result: dict[str, Any],
    now: datetime | None = None,
) -> OperationRead:
    current = (now or _utc_now()).astimezone(timezone.utc)
    read = store.read_operation(operation_key)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable("cannot record UNKNOWN for missing/corrupt operation")
    op = read.record
    if op.lane_id != lane_id:
        raise StateStoreError("operation/lane identity mismatch")
    op.state = "UNKNOWN"
    op.updated_at = _iso(current)
    op.reconciliation_required = True
    op.receipt = sanitize_receipt({**op.receipt, "result": result, "state": "UNKNOWN"})
    saved = store.compare_and_swap_operation(operation_key, read.version, op)

    ws = store.read_workstream(lane_id)
    if ws.status != "OK" or ws.record is None:
        raise StateUnavailable("operation is UNKNOWN but lane state is unavailable")
    runtime = ws.record
    runtime.unknown_write_state = {
        "operation_key": operation_key,
        "recorded_at": _iso(current),
        "reconciliation_required": True,
    }
    runtime.operation_receipt = op.receipt
    store.compare_and_swap_workstream(lane_id, ws.version, runtime)
    return saved


def record_authoritative_readback(
    store: StateStore,
    *,
    lane_id: str,
    operation_key: str,
    observed: bool | None,
    evidence: dict[str, Any],
    now: datetime | None = None,
) -> OperationRead:
    """Resolve ambiguous IN_FLIGHT/UNKNOWN only from authoritative post-state."""
    current = (now or _utc_now()).astimezone(timezone.utc)
    read = store.read_operation(operation_key)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable("operation state unavailable for readback")
    op = read.record
    if op.lane_id != lane_id:
        raise StateStoreError("operation/lane identity mismatch")
    if op.state not in {"IN_FLIGHT", "UNKNOWN"}:
        raise StateStoreError(
            "authoritative readback requires ambiguous IN_FLIGHT/UNKNOWN operation"
        )
    op.authoritative_readback = {
        "observed": observed,
        "evidence": sanitize_receipt(evidence),
        "read_at": _iso(current),
    }
    op.updated_at = _iso(current)
    if observed is True:
        op.state = "CONFIRMED"
        op.reconciliation_required = False
    elif observed is False:
        op.state = "RECONCILED_NOT_OBSERVED"
        op.reconciliation_required = False
    else:
        op.state = "UNKNOWN"
        op.reconciliation_required = True
    op.receipt = sanitize_receipt(
        {**op.receipt, "state": op.state, "post_condition": op.authoritative_readback}
    )
    saved = store.compare_and_swap_operation(operation_key, read.version, op)

    ws = store.read_workstream(lane_id)
    if ws.status != "OK" or ws.record is None:
        raise StateUnavailable("operation readback saved but lane state unavailable")
    runtime = ws.record
    runtime.operation_receipt = op.receipt
    if observed is None:
        runtime.unknown_write_state = {
            "operation_key": operation_key,
            "recorded_at": _iso(current),
            "reconciliation_required": True,
        }
    else:
        runtime.unknown_write_state = None
        if runtime.lease and runtime.lease.operation_key == operation_key:
            runtime.lease = None
            runtime.action_in_flight = None
    store.compare_and_swap_workstream(lane_id, ws.version, runtime)
    return saved
