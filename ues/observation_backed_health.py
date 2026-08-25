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


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("provider observation timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def observation_backed_no_effect_eligible(
    adapter: Mapping[str, Any],
    authority: Mapping[str, Any] | None,
) -> bool:
    """Return True only when a lifecycle cycle has no provider-routing topology."""

    runtime = legacy._lineage_runtime(adapter) or {}
    stable = runtime.get("workstreams")
    if isinstance(stable, Mapping) and any(isinstance(value, Mapping) for value in stable.values()):
        return False

    if isinstance(authority, Mapping):
        lineages = authority.get("lineages")
        if isinstance(lineages, Mapping) and any(isinstance(value, Mapping) for value in lineages.values()):
            return False

        generation = authority.get("generation_policy")
        generation = generation if isinstance(generation, Mapping) else {}
        for key in ("authorized_initial_lineages", "authorized_lineages"):
            entries = generation.get(key)
            entries = entries if isinstance(entries, Mapping) else {}
            if any(isinstance(value, Mapping) and value.get("authorized") is True for value in entries.values()):
                return False

        dispatches = authority.get("workflow_dispatches")
        dispatches = dispatches if isinstance(dispatches, Mapping) else {}
        if any(isinstance(value, Mapping) and value.get("authorized") is True for value in dispatches.values()):
            return False

    return True


def run_observation_backed_no_effect_health(
    adapter: Mapping[str, Any],
    *,
    authority: Mapping[str, Any] | None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist lifecycle health and expose sanitized terminal results from StateStore.

    This path performs no provider call and cannot route an effect. Terminal results
    are consumable only when the observer already proved exact session fingerprint,
    repository, durable lineage generation and reviewed/candidate SHA binding.
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
    observation_lane = canonical_lane_id(project, route, OBSERVATION_WORKSTREAM)
    read = store.read_workstream(observation_lane)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable(read.reason or "provider observation unavailable for no-effect health")

    provider_state = read.record.last_observed_provider_state
    if not isinstance(provider_state, Mapping):
        raise StateUnavailable("provider observation payload is missing")
    if provider_state.get("provider_read_complete") is not True:
        raise StateUnavailable("provider observation is not a complete authoritative read")
    if provider_state.get("provider_mutation_performed") is not False:
        raise StateUnavailable("provider observation is not read-only")
    if str(provider_state.get("repository") or "").casefold() != repository.casefold():
        raise StateUnavailable("provider observation repository binding does not match adapter")

    observed_at = str(provider_state.get("observed_at") or "").strip()
    try:
        age_seconds = max(
            0.0,
            ((now or datetime.now(timezone.utc)).astimezone(timezone.utc) - _parse_time(observed_at)).total_seconds(),
        )
    except (TypeError, ValueError) as exc:
        raise StateUnavailable("provider observation timestamp is invalid") from exc
    if age_seconds > stale_seconds:
        raise StateUnavailable("provider observation is stale for no-effect health")

    session_count = provider_state.get("session_count")
    if not isinstance(session_count, int) or isinstance(session_count, bool) or session_count < 0:
        raise StateUnavailable("provider observation session_count is invalid")

    raw_results = provider_state.get("results")
    results = [dict(item) for item in raw_results if isinstance(item, Mapping)] if isinstance(raw_results, list) else []
    state_counts = Counter(str(item.get("result_state") or "UNKNOWN") for item in results)
    consumable = [item for item in results if item.get("result_state") == "PARENT_CONSUMABLE"]

    runtime_binding = observed.runtime_binding_from_env()
    persist = observed._persist_health_with_runtime_binding(legacy._persist_health, runtime_binding)
    event_id = str((authority or {}).get("authority_event_id") or "").strip() or None
    summary = {
        "project": project,
        "runtime": "V2_OBSERVATION_BACKED_NO_EFFECT",
        "lineage_count": len(results),
        "provider_session_count": session_count,
        "provider_inventory_source": "STATESTORE_PROVIDER_OBSERVATION",
        "provider_observation_version": read.version,
        "provider_observation_age_seconds": age_seconds,
        "provider_live_read_performed": False,
        "binding_counts": dict(sorted(state_counts.items())),
        "terminal_result_count": len(results),
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
        "schema_version": "1.1",
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
