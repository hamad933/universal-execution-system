from __future__ import annotations

from typing import Any, Iterable, Mapping

from .identity import LaneKey, lane_id_from_key
from .lifecycle import ActionCapability
from .reconciliation import (
    WorkstreamBinding,
    reconcile_portfolio,
)
from .state_store import StateStore, StateStoreError
from .watchdog import evaluate_control_cycle

SCHEMA_VERSION = "2.0"
SHADOW_MODE = "SHADOW"


def _value(value: object) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw).strip()
    return text or None


def _shadow_route(*, capability: ActionCapability, action: object, stop_gate: object, issues: tuple[str, ...]) -> str:
    if stop_gate is not None or issues:
        return "STOP_GATE"
    if action is None:
        return "STOP_GATE"
    if capability is ActionCapability.EXTERNAL_EFFECT:
        return "OBSERVE_EXTERNAL_EFFECT_CANDIDATE"
    if capability is ActionCapability.CONTROL_SIGNAL:
        return "CONTROL_SIGNAL"
    return "READ_ONLY"


def run_shadow_cycle(
    bindings: Iterable[WorkstreamBinding],
    *,
    previous_by_lane: Mapping[LaneKey, WorkstreamBinding] | None = None,
    state_store: StateStore | None = None,
    watchdog_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconcile and route one portfolio observation cycle without external mutation.

    This Integration-owned loop is intentionally SHADOW-only. It may read runtime
    state, reconcile authoritative bindings, emit internal control signals and report
    semantic external-effect candidates, but it never claims a lease, writes state,
    sends a provider message, creates a task/session, changes GitHub, or interprets an
    activation mode as mutation authority.
    """

    items = tuple(bindings)
    reconciled = reconcile_portfolio(items, previous_by_lane=previous_by_lane)

    lane_results: list[dict[str, Any]] = []
    watchdog_lanes: list[dict[str, Any]] = []
    state_read_failures: list[str] = []
    external_effect_candidates: list[dict[str, str]] = []

    for result in reconciled:
        binding = result.binding
        lane_key = binding.lane_key
        lane_id = lane_id_from_key(lane_key) if lane_key is not None else None
        resolution = result.resolution
        capability = resolution.required_capability
        action = _value(resolution.action)
        stop_gate = _value(resolution.stop_gate)

        runtime_status = "NOT_REQUESTED"
        runtime_observed_mode: str | None = None
        runtime_mutation_allowed = False
        runtime_issue: str | None = None

        if state_store is not None and lane_id is not None:
            try:
                runtime = state_store.read_workstream(lane_id)
                runtime_status = runtime.status
                runtime_observed_mode = runtime.effective_activation_mode
                runtime_mutation_allowed = bool(runtime.mutation_allowed)
                if runtime.status not in {"OK", "MISSING"}:
                    runtime_issue = f"runtime_state:{runtime.status}"
            except StateStoreError as exc:
                runtime_status = "ERROR"
                runtime_issue = f"runtime_state_read_failed:{type(exc).__name__}"

        if runtime_issue is not None:
            state_read_failures.append(lane_id or "UNBOUND")

        route = _shadow_route(
            capability=capability,
            action=resolution.action,
            stop_gate=resolution.stop_gate,
            issues=result.issues,
        )
        if capability is ActionCapability.EXTERNAL_EFFECT and action and not result.issues:
            external_effect_candidates.append(
                {"lane_id": lane_id or "UNBOUND", "action": action}
            )

        blocked = bool(result.issues or resolution.stop_gate or runtime_issue)
        lane_result = {
            "lane_id": lane_id,
            "project": binding.project,
            "route": binding.route,
            "workstream": binding.workstream,
            "lifecycle_state": _value(binding.lifecycle_state),
            "semantic_action": action,
            "required_capability": capability.value,
            "stop_gate": stop_gate,
            "issues": list(result.issues),
            "shadow_route": route,
            "blocked": blocked,
            "candidate_sha_moved": result.candidate_sha_moved,
            "prior_review_invalidated": result.prior_review_invalidated,
            "prior_ci_invalidated": result.prior_ci_invalidated,
            "runtime_state_status": runtime_status,
            "runtime_observed_activation_mode": runtime_observed_mode,
            "runtime_reported_mutation_allowed": runtime_mutation_allowed,
            "runtime_issue": runtime_issue,
            "effective_activation_mode": SHADOW_MODE,
            "mutation_allowed": False,
            "external_effect_dispatched": False,
            "task_or_session_created": False,
        }
        lane_results.append(lane_result)

        watchdog_lanes.append(
            {
                "lane_id": lane_id or "UNBOUND",
                "blocked": blocked,
                "next_action": action if resolution.stop_gate is None else None,
                "stop_gate": stop_gate or ("RUNTIME_STATE_UNAVAILABLE" if runtime_issue else None),
                "auto_safe_incident_proven": False,
                "auto_safe_treated": False,
            }
        )

    health = evaluate_control_cycle(watchdog_lanes, policy=watchdog_policy)
    healthy = health["cycle_status"] == "CONTROL_CYCLE_OK" and not state_read_failures

    return {
        "schema_version": SCHEMA_VERSION,
        "activation_mode": SHADOW_MODE,
        "mutation_allowed": False,
        "external_effects_dispatched": 0,
        "tasks_or_sessions_created": 0,
        "lane_count": len(lane_results),
        "lanes": lane_results,
        "external_effect_candidates": external_effect_candidates,
        "state_read_failures": state_read_failures,
        "watchdog": health,
        "cycle_status": "CONTROL_CYCLE_OK" if healthy else "CONTROL_CYCLE_FAILED",
    }
