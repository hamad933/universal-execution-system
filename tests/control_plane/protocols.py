"""Test-only semantic protocols for Automation Control Plane V2 replay.

These protocols are integration expectations, not production architecture.
Parallel domains may expose different concrete APIs; the integration authority
can bind those APIs to these semantic operations without changing replay intent.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence


Decision = Mapping[str, Any]
Snapshot = Mapping[str, Any]


class LifecycleProtocol(Protocol):
    def resolve_next_transition(self, snapshot: Snapshot) -> Decision: ...


class ReconciliationProtocol(Protocol):
    def reconcile_binding(self, runtime: Snapshot, observed: Snapshot) -> Decision: ...


class RoutingProtocol(Protocol):
    def route(self, snapshot: Snapshot) -> Decision: ...


class WatchdogProtocol(Protocol):
    def evaluate(self, snapshot: Snapshot) -> Sequence[Decision]: ...


class TaskBudgetProtocol(Protocol):
    def classify(self, snapshot: Snapshot) -> Decision: ...


class JulesProviderProtocol(Protocol):
    def normalize_state(self, raw_state: str) -> str: ...


class GitHubProviderProtocol(Protocol):
    def read_evidence_binding(self, snapshot: Snapshot) -> Decision: ...


class MetricsProtocol(Protocol):
    def emit_sanitized_receipt(self, snapshot: Snapshot) -> Decision: ...


class RecoveryProtocol(Protocol):
    def recover_unknown_write(self, snapshot: Snapshot) -> Decision: ...


class OperationSafetyProtocol(Protocol):
    def reserve_operation(self, operation_key: str, snapshot: Snapshot) -> Decision: ...


EXPECTED_PRODUCTION_MODULES = (
    "ues.lifecycle",
    "ues.reconciliation",
    "ues.providers.jules",
    "ues.providers.github",
    "ues.routing",
    "ues.watchdog",
    "ues.task_budget",
    "ues.metrics",
    "ues.state_store",
    "ues.recovery",
    "ues.failures",
    "ues.idempotency",
    "ues.operation_records",
    "ues.transaction",
)
