from __future__ import annotations

import os
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping, Sequence

from . import lifecycle_runtime as legacy
from .initial_lineage_effects import _persist_initial_binding
from .jules_lifecycle import JulesLifecycleClient
from .lineage_registry import lineage_lane_id, session_fingerprint
from .live_runtime import build_live_state_store
from .providers.base import NetworkError, RateLimitError, ServerError
from .state_store import StateUnavailable, record_authoritative_readback, record_unknown_write
from .task_budget_accounting import record_confirmed_generation


_ROLE_KEYS = {
    "writer": "WRITER",
    "reviewer": "REVIEWER",
    "assurance": "ASSURANCE",
    "final_assurance": "ASSURANCE",
}
_TRANSIENT_PROVIDER_ERRORS = (NetworkError, RateLimitError, ServerError)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _source_for_repository(client: JulesLifecycleClient, repository: str) -> tuple[str | None, bool]:
    matches: list[str] = []
    for source in client.list_sources(page_size=100):
        if str(legacy._source_repository(source) or "").casefold() != repository.casefold():
            continue
        name = str(source.get("name") or "").strip().strip("/")
        if name:
            matches.append(name)
    unique = sorted(set(matches))
    return (unique[0], True) if len(unique) == 1 else (None, False)


def _matching_sessions(
    inventory: Sequence[Mapping[str, Any]],
    *,
    repository: str,
    starting_branch: str,
    transition_key: str,
) -> list[Mapping[str, Any]]:
    marker = f"[{transition_key[:12]}]"
    result: list[Mapping[str, Any]] = []
    for session in inventory:
        if str(session.get("_source_repository") or "").casefold() != repository.casefold():
            continue
        if str(session.get("sourceStartingBranch") or "") != starting_branch:
            continue
        title = str(session.get("title") or session.get("displayName") or "")
        if marker not in title or not str(session.get("name") or "").strip():
            continue
        result.append(session)
    return result


def _operation_identity(
    store: Any,
    *,
    lane_id: str,
    workstream: str,
    role: str,
) -> tuple[Any, Any, Mapping[str, Any]] | None:
    lane = store.read_workstream(lane_id)
    if lane.status != "OK" or lane.record is None:
        return None
    evidence = lane.record.evidence_bindings or {}
    if int(evidence.get("generation") or 0) != 0:
        return None
    if str(evidence.get("session_fingerprint") or "").strip():
        return None
    operation_key = str(
        lane.record.operation_key
        or (lane.record.operation_receipt or {}).get("operation_key")
        or ""
    ).strip()
    if not operation_key:
        return None
    operation = store.read_operation(operation_key)
    if operation.status != "OK" or operation.record is None:
        return None
    if operation.record.lane_id != lane_id:
        raise StateUnavailable("stale initial-lineage operation/lane identity mismatch")
    if operation.record.action != "create-initial-lineage-session":
        return None
    if operation.record.state not in {"IN_FLIGHT", "UNKNOWN"}:
        return None
    if operation.record.workstream_id != f"LINEAGE::{workstream}::{role}":
        raise StateUnavailable("stale initial-lineage workstream identity mismatch")
    receipt = operation.record.receipt if isinstance(operation.record.receipt, Mapping) else {}
    return lane, operation, receipt


def reconcile_stale_initial_lineage_lane(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    repository: str,
    authority_event_id: str,
    inventory: Sequence[Mapping[str, Any]],
    source_name: str,
    authority_starting_branch: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile an old generation-0 IN_FLIGHT/UNKNOWN create without provider mutation."""

    state_role = "ASSURANCE" if role.upper() == "FINAL_ASSURANCE" else role.upper()
    lane_id = lineage_lane_id(project, route, workstream, state_role)
    identity = _operation_identity(store, lane_id=lane_id, workstream=workstream, role=state_role)
    if identity is None:
        return {
            "workstream": workstream,
            "role": state_role,
            "decision": "NO_STALE_INITIAL_LINEAGE_OPERATION",
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }
    lane, operation, receipt = identity
    operation_key = operation.record.operation_key

    transition_key = str(receipt.get("transition_key") or "").strip()
    starting_branch = str(receipt.get("starting_branch") or "").strip()
    task_spec_digest = str(receipt.get("task_spec_digest") or "").strip()
    source_fingerprint = str(receipt.get("source_fingerprint") or "").strip()
    creation_kind = str(receipt.get("creation_kind") or "").strip().upper()
    generation = int(receipt.get("generation") or 0)
    effect_identity = receipt.get("effect_identity") if isinstance(receipt.get("effect_identity"), Mapping) else {}
    target = effect_identity.get("target") if isinstance(effect_identity.get("target"), Mapping) else {}

    if not all((transition_key, starting_branch, task_spec_digest, source_fingerprint)):
        return {
            "workstream": workstream,
            "role": state_role,
            "decision": "STALE_INITIAL_LINEAGE_DURABLE_IDENTITY_INCOMPLETE",
            "provider_write_attempted": False,
            "operation_key": operation_key,
            "safe_to_blind_retry": False,
        }
    if creation_kind != "INITIAL_LOGICAL_LINEAGE" or generation != 1:
        raise StateUnavailable("stale initial-lineage operation receipt has unexpected creation identity")
    if sha256(source_name.encode("utf-8")).hexdigest() != source_fingerprint:
        raise StateUnavailable("stale initial-lineage source fingerprint mismatch")
    if authority_starting_branch and authority_starting_branch != starting_branch:
        return {
            "workstream": workstream,
            "role": state_role,
            "decision": "STALE_INITIAL_LINEAGE_AUTHORITY_BRANCH_MISMATCH",
            "provider_write_attempted": False,
            "operation_key": operation_key,
            "safe_to_blind_retry": False,
        }
    if target:
        if str(target.get("transition_key") or "") != transition_key:
            raise StateUnavailable("stale initial-lineage effect transition mismatch")
        if str(target.get("starting_branch") or "") != starting_branch:
            raise StateUnavailable("stale initial-lineage effect branch mismatch")
        if str(target.get("source_fingerprint") or "") != source_fingerprint:
            raise StateUnavailable("stale initial-lineage effect source mismatch")
        if str(target.get("role") or "").upper() != state_role:
            raise StateUnavailable("stale initial-lineage effect role mismatch")
        if str(target.get("generation") or "") != "1":
            raise StateUnavailable("stale initial-lineage effect generation mismatch")

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lease = lane.record.lease
    if lease is not None and lease.operation_key == operation_key:
        try:
            if _parse_time(lease.expires_at) > current:
                return {
                    "workstream": workstream,
                    "role": state_role,
                    "decision": "STALE_INITIAL_LINEAGE_OPERATION_STILL_LEASED",
                    "provider_write_attempted": False,
                    "operation_key": operation_key,
                    "safe_to_blind_retry": False,
                }
        except ValueError as exc:
            raise StateUnavailable("stale initial-lineage lease timestamp is invalid") from exc

    matches = _matching_sessions(
        inventory,
        repository=repository,
        starting_branch=starting_branch,
        transition_key=transition_key,
    )
    if not matches:
        return {
            "workstream": workstream,
            "role": state_role,
            "decision": "STALE_INITIAL_LINEAGE_NOT_YET_OBSERVED",
            "provider_write_attempted": False,
            "match_count": 0,
            "operation_key": operation_key,
            "transition_key": transition_key,
            "safe_to_blind_retry": False,
        }
    if len(matches) != 1:
        return {
            "workstream": workstream,
            "role": state_role,
            "decision": "STALE_INITIAL_LINEAGE_AMBIGUOUS_DUPLICATE",
            "provider_write_attempted": False,
            "match_count": len(matches),
            "operation_key": operation_key,
            "transition_key": transition_key,
            "safe_to_blind_retry": False,
        }

    session_name = str(matches[0].get("name") or "").strip()
    session_fp = session_fingerprint(session_name)
    candidate_sha = str((lane.record.evidence_bindings or {}).get("current_candidate_sha") or "").strip() or None
    event_id = str(authority_event_id or "").strip()
    if not event_id:
        return {
            "workstream": workstream,
            "role": state_role,
            "decision": "STALE_INITIAL_LINEAGE_CURRENT_AUTHORITY_REQUIRED",
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }

    record_authoritative_readback(
        store,
        lane_id=lane_id,
        operation_key=operation_key,
        observed=True,
        evidence={
            "outcome": "INITIAL_LINEAGE_SESSION_CREATED_RECONCILED_FROM_STALE_OPERATION",
            "session_fingerprint": session_fp,
            "repository": repository,
            "starting_branch": starting_branch,
            "generation": 1,
            "creation_kind": "INITIAL_LOGICAL_LINEAGE",
            "initial_lineage_transition_key": transition_key,
            "provider_title_marker": transition_key[:12],
            "authoritative_provider_enumeration": True,
            "raw_session_id_persisted": False,
            "safe_to_blind_retry": False,
        },
    )
    try:
        binding = _persist_initial_binding(
            store,
            lane_id=lane_id,
            role=state_role,
            workstream=workstream,
            session_fp=session_fp,
            source_name=source_name,
            repository=repository,
            starting_branch=starting_branch,
            authority_event_id=event_id,
            operation_key=operation_key,
            transition_key=transition_key,
            candidate_sha=candidate_sha,
            task_spec_digest=task_spec_digest,
            policy_provenance={
                "source": "STALE_INITIAL_LINEAGE_RECONCILIATION",
                "authority_event_id": event_id,
            },
        )
        accounting = record_confirmed_generation(
            store,
            project=project,
            route=route,
            operation_key=operation_key,
            generation_transition_key=transition_key,
        )
    except Exception as exc:
        record_unknown_write(
            store,
            lane_id=lane_id,
            operation_key=operation_key,
            result={
                "category": "STALE_INITIAL_LINEAGE_STATESTORE_HANDOFF_FAILED",
                "error_type": type(exc).__name__,
                "initial_lineage_transition_key": transition_key,
                "safe_to_blind_retry": False,
            },
        )
        return {
            "workstream": workstream,
            "role": state_role,
            "decision": "STALE_INITIAL_LINEAGE_RECONCILED_STATESTORE_BINDING_REQUIRED",
            "provider_write_attempted": False,
            "match_count": 1,
            "operation_key": operation_key,
            "transition_key": transition_key,
            "safe_to_blind_retry": False,
        }

    return {
        "workstream": workstream,
        "role": state_role,
        "decision": "STALE_INITIAL_LINEAGE_AUTHORITATIVELY_RECONCILED",
        "provider_write_attempted": False,
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "match_count": 1,
        "operation_key": operation_key,
        "transition_key": transition_key,
        "generation": 1,
        "session_fingerprint": session_fp,
        "generation_binding": binding,
        "budget_accounting": accounting,
        "raw_session_id_persisted": False,
        "safe_to_blind_retry": False,
    }


def reconcile_project_stale_initial_lineages(
    adapter: Mapping[str, Any],
    authority: Mapping[str, Any] | None,
    *,
    store: Any | None = None,
    client: JulesLifecycleClient | None = None,
) -> dict[str, Any]:
    """Recover stale existing initial-lineage effects before any replacement create path."""

    if not isinstance(authority, Mapping):
        return {"result": "NO_CURRENT_AUTHORITY", "reconciled_count": 0, "results": []}
    project = str(adapter.get("project") or "").strip().upper()
    route = str(adapter.get("route") or project).strip()
    repository = str(adapter.get("repository") or "").strip()
    event_id = str(authority.get("authority_event_id") or "").strip()
    lineages = authority.get("lineages") if isinstance(authority.get("lineages"), Mapping) else {}
    if not project or not route or not repository or not event_id or not lineages:
        return {"result": "NO_ELIGIBLE_AUTHORITY_TOPOLOGY", "reconciled_count": 0, "results": []}

    state_store = store or build_live_state_store()
    candidates: list[tuple[str, str, str | None]] = []
    for workstream, topology in sorted(lineages.items(), key=lambda item: str(item[0])):
        if not isinstance(topology, Mapping):
            continue
        for role_key, state_role in _ROLE_KEYS.items():
            config = topology.get(role_key)
            if not isinstance(config, Mapping):
                continue
            lane_id = lineage_lane_id(project, route, str(workstream), state_role)
            identity = _operation_identity(
                state_store,
                lane_id=lane_id,
                workstream=str(workstream),
                role=state_role,
            )
            if identity is None:
                continue
            authority_branch = str(config.get("provider_starting_branch") or "").strip() or None
            candidates.append((str(workstream), state_role, authority_branch))

    if not candidates:
        return {
            "result": "NO_STALE_INITIAL_LINEAGE_OPERATIONS",
            "reconciled_count": 0,
            "provider_write_attempted": False,
            "results": [],
        }

    jules = client
    if jules is None:
        key = str(os.environ.get("JULES_API_KEY") or "").strip()
        if not key:
            return {
                "result": "STALE_INITIAL_LINEAGE_PROVIDER_CREDENTIAL_UNAVAILABLE",
                "reconciled_count": 0,
                "provider_write_attempted": False,
                "results": [],
            }
        jules = JulesLifecycleClient(key)
    try:
        inventory = legacy._provider_inventory(jules)
        source_name, source_proven = _source_for_repository(jules, repository)
    except _TRANSIENT_PROVIDER_ERRORS as exc:
        return {
            "result": "STALE_INITIAL_LINEAGE_PROVIDER_READ_UNAVAILABLE",
            "provider_read_error_category": getattr(exc, "category", type(exc).__name__),
            "provider_write_attempted": False,
            "reconciled_count": 0,
            "results": [],
            "safe_to_blind_retry": False,
        }
    if not source_name or not source_proven:
        return {
            "result": "STALE_INITIAL_LINEAGE_EXACT_SOURCE_REQUIRED",
            "provider_write_attempted": False,
            "reconciled_count": 0,
            "results": [],
            "safe_to_blind_retry": False,
        }

    results = [
        reconcile_stale_initial_lineage_lane(
            state_store,
            project=project,
            route=route,
            workstream=workstream,
            role=role,
            repository=repository,
            authority_event_id=event_id,
            inventory=inventory,
            source_name=source_name,
            authority_starting_branch=authority_branch,
        )
        for workstream, role, authority_branch in candidates
    ]
    reconciled = sum(1 for result in results if result.get("decision") == "STALE_INITIAL_LINEAGE_AUTHORITATIVELY_RECONCILED")
    return {
        "result": "STALE_INITIAL_LINEAGE_RECONCILIATION_COMPLETE",
        "candidate_count": len(candidates),
        "reconciled_count": reconciled,
        "provider_write_attempted": False,
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "results": results,
        "safe_to_blind_retry": False,
    }
