from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import terminal_recovery as recovery
from . import terminal_results
from .state_store import StateStoreError, StateUnavailable, record_authoritative_readback
from .task_budget_accounting import record_confirmed_generation


def _exact_pending_operation_binding(
    store: Any,
    *,
    candidate: Mapping[str, Any],
    session_fp: str,
    bind: Any,
) -> dict[str, Any]:
    """Reconcile a pending initial-generation operation before binding its session.

    The caller has already proved a unique provider repository + starting-branch +
    exact transition-title-marker match. This wrapper additionally requires the
    durable operation identity/effect target to agree with that pending transition,
    records authoritative provider readback through the canonical operation state
    machine, then delegates lineage binding to the existing terminal recovery CAS.
    It never clears IN_FLIGHT/UNKNOWN state by itself and never repeats a provider
    effect.
    """

    lane_id = str(candidate.get("lane_id") or "").strip()
    pending = candidate.get("pending")
    snapshot = candidate.get("record")
    if not lane_id or not isinstance(pending, Mapping) or snapshot is None:
        return {
            "state": "IDENTITY_OPERATION_PROOF_INCOMPLETE",
            "cas_performed": False,
            "authoritative_readback": False,
        }

    transition_key = str(pending.get("transition_key") or "").strip()
    starting_branch = str(pending.get("starting_branch") or "").strip()
    repository = str(pending.get("source_repository") or "").strip()
    generation = int(pending.get("next_generation") or 1)
    role = str((getattr(snapshot, "evidence_bindings", None) or {}).get("role") or "").upper()
    operation_key = str(getattr(snapshot, "operation_key", None) or "").strip()
    action = getattr(snapshot, "action_in_flight", None)
    if isinstance(action, Mapping):
        action_key = str(action.get("operation_key") or "").strip()
        if operation_key and action_key and operation_key != action_key:
            return {
                "state": "IDENTITY_OPERATION_KEY_MISMATCH",
                "cas_performed": False,
                "authoritative_readback": False,
            }
        operation_key = operation_key or action_key

    if not all((transition_key, starting_branch, repository, role, operation_key, session_fp)):
        return {
            "state": "IDENTITY_OPERATION_PROOF_INCOMPLETE",
            "cas_performed": False,
            "authoritative_readback": False,
        }

    operation = store.read_operation(operation_key)
    if operation.status != "OK" or operation.record is None:
        return {
            "state": "IDENTITY_OPERATION_STATE_UNAVAILABLE",
            "cas_performed": False,
            "authoritative_readback": False,
        }
    op = operation.record
    effect = op.effect_identity if isinstance(op.effect_identity, Mapping) else {}
    target = effect.get("target") if isinstance(effect.get("target"), Mapping) else {}
    exact_operation = (
        op.lane_id == lane_id
        and op.state in {"IN_FLIGHT", "UNKNOWN"}
        and str(effect.get("project") or "") == str(getattr(snapshot, "project", "") or "")
        and str(effect.get("route") or "") == str(getattr(snapshot, "route", "") or "")
        and str(target.get("creation_kind") or "").upper() == "INITIAL_LOGICAL_LINEAGE"
        and str(target.get("transition_key") or "") == transition_key
        and str(target.get("starting_branch") or "") == starting_branch
        and str(target.get("role") or "").upper() == role
        and int(target.get("generation") or 0) == generation
    )
    if not exact_operation:
        return {
            "state": "IDENTITY_OPERATION_EFFECT_MISMATCH",
            "cas_performed": False,
            "authoritative_readback": True,
        }

    try:
        record_authoritative_readback(
            store,
            lane_id=lane_id,
            operation_key=operation_key,
            observed=True,
            evidence={
                "outcome": "INITIAL_LINEAGE_SESSION_CREATED_RECONCILED_BY_TERMINAL_READBACK",
                "session_fingerprint": session_fp,
                "repository": repository,
                "starting_branch": starting_branch,
                "generation": generation,
                "creation_kind": "INITIAL_LOGICAL_LINEAGE",
                "initial_lineage_transition_key": transition_key,
                "authoritative_provider_enumeration": True,
                "raw_session_id_persisted": False,
                "raw_title_persisted": False,
                "safe_to_blind_retry": False,
            },
        )
    except (StateStoreError, StateUnavailable):
        return {
            "state": "IDENTITY_OPERATION_READBACK_RECONCILIATION_REQUIRED",
            "cas_performed": False,
            "authoritative_readback": False,
        }

    binding = bind(store, candidate=candidate, session_fp=session_fp)
    if binding.get("state") not in {
        "IDENTITY_EXACTLY_BOUND",
        "IDENTITY_ALREADY_BOUND",
        "IDENTITY_CONCURRENTLY_BOUND",
        "IDENTITY_BOUND_READBACK_RECONCILED",
    }:
        return binding

    try:
        accounting = record_confirmed_generation(
            store,
            project=str(getattr(snapshot, "project", "") or ""),
            route=str(getattr(snapshot, "route", "") or ""),
            operation_key=operation_key,
            generation_transition_key=transition_key,
        )
    except (StateStoreError, StateUnavailable):
        return {
            "state": "IDENTITY_BOUND_BUDGET_ACCOUNTING_RECONCILIATION_REQUIRED",
            "cas_performed": bool(binding.get("cas_performed")),
            "authoritative_readback": True,
        }
    return {**binding, "budget_accounting": accounting}


def run_read_only_backfill(
    project_names: Sequence[str] | None = None,
    *,
    store: Any | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Run terminal backfill with pre-read identity caching and governed reconciliation.

    The cache is read-only identity evidence captured from StateStore before Jules
    content reads. It is used only if StateStore discovery becomes unavailable after
    provider content was already read. Persistence and authoritative readback still
    use the live StateStore and therefore remain fail-closed; cached state can never
    make a CAS appear successful.

    Pending initial-generation identities are not allowed to clear an ambiguous
    operation merely because a provider title matched. The canonical operation
    readback is confirmed first, then the existing exact lineage CAS is reused.
    """

    canonical_index = terminal_results.lineage_index
    original_results_index = terminal_results.lineage_index
    original_recovery_index = recovery.lineage_index
    original_bind = recovery._bind_pending_identity_once
    cache: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}

    def cached_index(live_store: Any, *, project: str, route: str) -> dict[str, list[dict[str, Any]]]:
        key = (str(project), str(route))
        try:
            value = canonical_index(live_store, project=project, route=route)
        except StateUnavailable:
            if key in cache:
                return cache[key]
            raise
        # Never replace a previously proven non-empty exact identity cache with an
        # empty view produced during a later partial read outage. An empty first
        # read remains authoritative and cannot be upgraded by inference.
        if value or key not in cache:
            cache[key] = {
                str(fp): [dict(item) for item in matches]
                for fp, matches in value.items()
            }
        return value

    def governed_bind(live_store: Any, *, candidate: Mapping[str, Any], session_fp: str) -> dict[str, Any]:
        return _exact_pending_operation_binding(
            live_store,
            candidate=candidate,
            session_fp=session_fp,
            bind=original_bind,
        )

    terminal_results.lineage_index = cached_index
    recovery.lineage_index = cached_index
    recovery._bind_pending_identity_once = governed_bind
    try:
        return recovery.run_read_only_backfill(
            project_names,
            store=store,
            client=client,
        )
    finally:
        terminal_results.lineage_index = original_results_index
        recovery.lineage_index = original_recovery_index
        recovery._bind_pending_identity_once = original_bind
