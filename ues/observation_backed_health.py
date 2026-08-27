from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

from . import lifecycle_runtime as legacy
from . import lifecycle_runtime_observed as observed
from .identity import canonical_lane_id
from .live_runtime import build_live_state_store
from .provider_observer import DEFAULT_STALE_SECONDS, OBSERVATION_WORKSTREAM
from .state_store import StateUnavailable
from .terminal_recovery import read_persisted_terminal_results


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("provider observation timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _latest_confirmed_lineage_effect_at(
    store: Any,
    *,
    project: str,
    route: str,
) -> datetime | None:
    """Return the newest exact lineage-generation provider readback timestamp.

    A provider inventory snapshot that started before a confirmed physical generation
    can be young by wall-clock age while still being logically stale. Only durable,
    exact CONFIRMED lineage operation receipts participate here; titles, labels and
    provider heuristics never do.
    """

    discover = getattr(store, "discover_lane_ids", None)
    if not callable(discover):
        return None
    latest: datetime | None = None
    for lane_id in discover():
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            continue
        record = read.record
        if record.project != project or record.route != route:
            continue
        if not str(record.workstream_id or "").startswith("LINEAGE::"):
            continue
        receipt = record.operation_receipt if isinstance(record.operation_receipt, Mapping) else {}
        if str(receipt.get("state") or "").upper() != "CONFIRMED":
            continue
        if int(receipt.get("generation") or 0) <= 0:
            continue
        post = receipt.get("post_condition") if isinstance(receipt.get("post_condition"), Mapping) else {}
        if post.get("observed") is not True:
            continue
        read_at = str(post.get("read_at") or "").strip()
        if not read_at:
            continue
        try:
            timestamp = _parse_time(read_at)
        except (TypeError, ValueError):
            continue
        if latest is None or timestamp > latest:
            latest = timestamp
    return latest


def observation_backed_no_effect_eligible(
    adapter: Mapping[str, Any],
    authority: Mapping[str, Any] | None,
) -> bool:
    """Return True only when Current Authority explicitly proves zero provider effects.

    Dynamic lineage declarations are identity/reconciliation topology, not effects by
    themselves. When such lineages are present, both generation authorization flags
    must be explicitly false before the fresh persisted provider observation may be
    used. Missing/ambiguous generation policy therefore stays on the live provider
    path, preserving fail-closed behavior for any potentially effect-capable cycle.
    """

    runtime = legacy._lineage_runtime(adapter) or {}
    stable = runtime.get("workstreams")
    if isinstance(stable, Mapping) and any(isinstance(value, Mapping) for value in stable.values()):
        return False

    if isinstance(authority, Mapping):
        lineages = authority.get("lineages")
        lineages_present = isinstance(lineages, Mapping) and any(
            isinstance(value, Mapping) for value in lineages.values()
        )

        generation = authority.get("generation_policy")
        generation = generation if isinstance(generation, Mapping) else {}
        if lineages_present and not (
            generation.get("necessary_generation_authorized") is False
            and generation.get("generation_effect_authorized") is False
        ):
            return False

        if generation.get("necessary_generation_authorized") is True:
            return False
        if generation.get("generation_effect_authorized") is True:
            return False
        for key in ("authorized_initial_lineages", "authorized_lineages"):
            entries = generation.get(key)
            entries = entries if isinstance(entries, Mapping) else {}
            if any(isinstance(value, Mapping) and value.get("authorized") is True for value in entries.values()):
                return False

        dispatches = authority.get("workflow_dispatches")
        dispatches = dispatches if isinstance(dispatches, Mapping) else {}
        if any(isinstance(value, Mapping) and value.get("authorized") is True for value in dispatches.values()):
            return False

        # Controller-resolvable waiting responses are provider-routing authority:
        # lifecycle_runtime_current may turn one into a bounded same-session Jules
        # message.  Treat only entries that satisfy that runtime's exact response
        # preconditions as effect-capable; malformed, empty, scope-expanding, or
        # non-controller-resolvable entries do not manufacture authority.
        waiting = authority.get("waiting_responses")
        waiting = waiting if isinstance(waiting, Mapping) else {}
        if any(
            isinstance(entry, Mapping)
            and entry.get("controller_resolvable") is True
            and entry.get("scope_expansion") is not True
            and bool(str(entry.get("response") or "").strip())
            for entry in waiting.values()
        ):
            return False

    return True


def _result_key(item: Mapping[str, Any]) -> tuple[str, str, str, int]:
    return (
        str(item.get("session_fingerprint") or ""),
        str(item.get("logical_workstream") or ""),
        str(item.get("role") or "").upper(),
        int(item.get("generation") or 0),
    )


def run_observation_backed_no_effect_health(
    adapter: Mapping[str, Any],
    *,
    authority: Mapping[str, Any] | None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Expose Parent-consumable terminal results without trusting raced inventory.

    Durable terminal results live on exact lineage records. The aggregate provider
    observation remains useful inventory/freshness evidence, but it is eligible for
    result materialization only when it is both age-fresh and newer than every exact
    confirmed lineage-generation effect already present in StateStore.
    """

    if not observation_backed_no_effect_eligible(adapter, authority):
        raise StateUnavailable("observation-backed health requires an effect-free empty topology")
    if stale_seconds <= 0:
        raise ValueError("stale_seconds must be positive")

    project = str(adapter.get("project") or "").strip()
    route = str(adapter.get("route") or project).strip()
    repository = str(adapter.get("repository") or "").strip()
    if not project or not route or not repository:
        raise StateUnavailable("adapter identity is incomplete for observation-backed health")

    store = build_live_state_store()
    persisted_results = read_persisted_terminal_results(
        store,
        project=project,
        route=route,
        repository=repository,
    )
    latest_lineage_effect_at = _latest_confirmed_lineage_effect_at(
        store,
        project=project,
        route=route,
    )

    observation_lane = canonical_lane_id(project, route, OBSERVATION_WORKSTREAM)
    read = store.read_workstream(observation_lane)
    provider_results: list[dict[str, Any]] = []
    provider_observation_available = False
    provider_observation_fresh = False
    provider_observation_reason: str | None = None
    provider_observation_version: int | None = None
    provider_observation_age_seconds: float | None = None
    provider_observation_at: datetime | None = None
    session_count: int | None = None

    if read.status == "OK" and read.record is not None:
        provider_state = read.record.last_observed_provider_state
        if not isinstance(provider_state, Mapping):
            provider_observation_reason = "PROVIDER_OBSERVATION_PAYLOAD_MISSING"
        elif provider_state.get("provider_read_complete") is not True:
            provider_observation_reason = "PROVIDER_OBSERVATION_READ_INCOMPLETE"
        elif provider_state.get("provider_mutation_performed") is not False:
            provider_observation_reason = "PROVIDER_OBSERVATION_NOT_READ_ONLY"
        elif str(provider_state.get("repository") or "").casefold() != repository.casefold():
            provider_observation_reason = "PROVIDER_OBSERVATION_REPOSITORY_MISMATCH"
        else:
            provider_observation_available = True
            provider_observation_version = read.version
            observed_at = str(provider_state.get("observed_at") or "").strip()
            try:
                provider_observation_at = _parse_time(observed_at)
                provider_observation_age_seconds = max(
                    0.0,
                    (
                        (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
                        - provider_observation_at
                    ).total_seconds(),
                )
                provider_observation_fresh = provider_observation_age_seconds <= stale_seconds
                if not provider_observation_fresh:
                    provider_observation_reason = "PROVIDER_OBSERVATION_STALE"
                elif (
                    latest_lineage_effect_at is not None
                    and provider_observation_at < latest_lineage_effect_at
                ):
                    provider_observation_fresh = False
                    provider_observation_reason = "PROVIDER_OBSERVATION_PREDATES_LATEST_LINEAGE_EFFECT"
            except (TypeError, ValueError):
                provider_observation_reason = "PROVIDER_OBSERVATION_TIMESTAMP_INVALID"
            raw_session_count = provider_state.get("session_count")
            if isinstance(raw_session_count, int) and not isinstance(raw_session_count, bool) and raw_session_count >= 0:
                session_count = raw_session_count
            else:
                provider_observation_reason = provider_observation_reason or "PROVIDER_OBSERVATION_SESSION_COUNT_INVALID"
            raw_results = provider_state.get("results")
            if provider_observation_fresh and isinstance(raw_results, list):
                provider_results = [dict(item) for item in raw_results if isinstance(item, Mapping)]
    else:
        provider_observation_reason = read.reason or f"PROVIDER_OBSERVATION_{read.status}"

    if not persisted_results and not provider_observation_available:
        raise StateUnavailable(provider_observation_reason or "provider observation and terminal results unavailable")

    merged: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for item in provider_results:
        merged[_result_key(item)] = item
    # Exact durable lineage results override the aggregate mirror because they
    # survive observer publication failure and are revalidated against current SHA.
    for item in persisted_results:
        merged[_result_key(item)] = dict(item)
    results = list(merged.values())
    results.sort(key=lambda item: (str(item.get("logical_workstream") or ""), str(item.get("role") or ""), int(item.get("generation") or 0)))

    state_counts = Counter(str(item.get("result_state") or "UNKNOWN") for item in results)
    consumable = [item for item in results if item.get("result_state") == "PARENT_CONSUMABLE"]

    runtime_binding = observed.runtime_binding_from_env()
    persist = observed._persist_health_with_runtime_binding(legacy._persist_health, runtime_binding)
    event_id = str((authority or {}).get("authority_event_id") or "").strip() or None
    if provider_observation_fresh and persisted_results:
        inventory_source = "STATESTORE_PROVIDER_OBSERVATION_PLUS_DURABLE_LINEAGE_RESULTS"
    elif persisted_results:
        inventory_source = "STATESTORE_DURABLE_LINEAGE_RESULTS_ONLY"
    elif provider_observation_fresh:
        inventory_source = "STATESTORE_PROVIDER_OBSERVATION"
    else:
        inventory_source = "STATESTORE_PROVIDER_OBSERVATION_REJECTED_AS_STALE_RELATIVE_TO_LINEAGE"
    summary = {
        "project": project,
        "runtime": "V2_OBSERVATION_BACKED_NO_EFFECT",
        "lineage_count": len(results),
        "provider_session_count": session_count,
        "provider_inventory_source": inventory_source,
        "provider_observation_available": provider_observation_available,
        "provider_observation_fresh": provider_observation_fresh,
        "provider_observation_reason": provider_observation_reason,
        "provider_observation_version": provider_observation_version,
        "provider_observation_age_seconds": provider_observation_age_seconds,
        "provider_observation_observed_at": (
            provider_observation_at.isoformat().replace("+00:00", "Z")
            if provider_observation_at is not None
            else None
        ),
        "latest_confirmed_lineage_effect_at": (
            latest_lineage_effect_at.isoformat().replace("+00:00", "Z")
            if latest_lineage_effect_at is not None
            else None
        ),
        "provider_live_read_performed": False,
        "binding_counts": dict(sorted(state_counts.items())),
        "terminal_result_count": len(results),
        "durable_lineage_terminal_result_count": len(persisted_results),
        "parent_consumable_result_count": len(consumable),
        "terminal_unconsumed_result_count": len(results) - len(consumable),
        "recovery_action_counts": {},
        "effect_decision_counts": {},
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "current_authority_loaded": authority is not None,
        "current_authority_event_id": event_id,
        "event_wakeup": None,
        "adapter_mutable_snapshot_is_authority": False,
        "state_store_generation_recovery_enabled": True,
        "same_session_reuse_first": True,
        "blocked_lane_freezes_independent_lanes": False,
    }

    persist(
        store,
        project=project,
        route=route,
        status="IN_FLIGHT",
        summary={
            "phase": "START",
            "runtime": "V2_OBSERVATION_BACKED_NO_EFFECT",
            "provider_live_read_performed": False,
        },
    )
    health = persist(store, project=project, route=route, status="PASS", summary=summary)
    return {
        "schema_version": "1.2",
        "project": project,
        "route": route,
        "result": "OBSERVATION_BACKED_NO_EFFECT_LIFECYCLE_COMPLETE",
        "summary": summary,
        "results": results,
        "health": health,
        "current_authority_loaded": authority is not None,
        "current_authority_event_id": event_id,
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "provider_live_read_performed": False,
        "provider_mutation_performed": False,
        "safe_to_blind_retry": False,
        "observed_runtime_binding": runtime_binding,
        "runtime_binding_grants_authority": False,
    }
