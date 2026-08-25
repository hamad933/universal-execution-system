from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .task_budget import evaluate_task_budget


class PolicyResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedExecutionPolicy:
    project: str
    route: str
    authority_event_id: str | None
    ceiling: int | None
    reserve_target: int
    reserve_is_hard: bool
    unknown_quota_window_policy: str
    necessary_generation_authorized: bool
    generation_effect_authorized: bool
    budget: Mapping[str, Any]
    provenance: Mapping[str, Any]

    @property
    def unknown_lifetime_policy(self) -> str:
        """Compatibility alias; policy now applies to current quota window only."""
        return self.unknown_quota_window_policy

    @property
    def generation_budget_safe(self) -> bool:
        return bool(self.budget.get("budget_allows_new_task"))

    @property
    def generation_allowed(self) -> bool:
        return bool(
            self.necessary_generation_authorized
            and self.generation_effect_authorized
            and self.generation_budget_safe
            and not self.budget.get("hard_ceiling_reached")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.1",
            "project": self.project,
            "route": self.route,
            "authority_event_id": self.authority_event_id,
            "ceiling": self.ceiling,
            "reserve_target": self.reserve_target,
            "reserve_is_hard": self.reserve_is_hard,
            "unknown_quota_window_policy": self.unknown_quota_window_policy,
            # Compatibility output; semantics are no longer lifetime-based.
            "unknown_lifetime_policy": self.unknown_quota_window_policy,
            "necessary_generation_authorized": self.necessary_generation_authorized,
            "generation_effect_authorized": self.generation_effect_authorized,
            "generation_budget_safe": self.generation_budget_safe,
            "generation_allowed": self.generation_allowed,
            "budget": dict(self.budget),
            "provenance": dict(self.provenance),
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value is not None:
            return value
    return default


def _integer(value: Any, name: str, *, default: int = 0) -> int:
    if value is None:
        return default
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PolicyResolutionError(f"{name} must be an integer") from exc
    if result < 0:
        raise PolicyResolutionError(f"{name} must be non-negative")
    return result


def resolve_execution_policy(
    *,
    adapter: Mapping[str, Any],
    governed_authority: Mapping[str, Any] | None,
    provider_observation: Mapping[str, Any] | None,
    state_snapshot: Mapping[str, Any] | None = None,
) -> ResolvedExecutionPolicy:
    """Resolve mutable execution policy for one cycle from current truth owners.

    Capacity is evaluated against the provider's *current quota window* only.
    Historical task/session inventory is audit/reconciliation evidence and must
    never reduce current-window headroom.

    Precedence is intentionally asymmetric:
    stable adapter defaults < current governed project authority < direct provider
    observation < durable StateStore effect state.
    """

    project = str(adapter.get("project") or "").strip()
    route = str(adapter.get("route") or project).strip()
    if not project or not route:
        raise PolicyResolutionError("adapter project and route are required")

    stable_budget = _mapping(adapter.get("task_budget"))
    authority = _mapping(governed_authority)
    current_budget = _mapping(authority.get("task_budget"))
    generation_policy = _mapping(authority.get("generation_policy"))
    provider = _mapping(provider_observation)
    state = _mapping(state_snapshot)

    authority_event_id = str(
        _first(authority.get("authority_event_id"), authority.get("event_id"), default="") or ""
    ).strip() or None
    authority_is_current = bool(authority and authority.get("current", True) and authority_event_id)

    ceiling_value = stable_budget.get("ceiling")
    ceiling_source = "adapter.default" if ceiling_value is not None else "unresolved"
    if authority_is_current and current_budget.get("ceiling") is not None:
        ceiling_value = current_budget.get("ceiling")
        ceiling_source = "governed_authority.task_budget.ceiling"
    ceiling = _integer(ceiling_value, "ceiling") if ceiling_value is not None else None
    ceiling_resolved = ceiling is not None

    reserve_source = "adapter.default"
    reserve_value = _first(stable_budget.get("reserve_target"), stable_budget.get("reserve"), default=0)
    if authority_is_current and (
        current_budget.get("reserve_target") is not None or current_budget.get("reserve") is not None
    ):
        reserve_value = _first(current_budget.get("reserve_target"), current_budget.get("reserve"), default=0)
        reserve_source = "governed_authority.task_budget.reserve"
    reserve_target = _integer(reserve_value, "reserve_target")
    if ceiling is not None and reserve_target > ceiling:
        raise PolicyResolutionError("reserve_target cannot exceed ceiling")

    reserve_is_hard = bool(
        _first(
            current_budget.get("reserve_is_hard") if authority_is_current else None,
            stable_budget.get("reserve_is_hard"),
            default=False,
        )
    )

    # New name wins; legacy name remains a compatibility alias for existing
    # adapters/current-authority payloads during migration.
    unknown_source = "adapter.default"
    stable_unknown = _first(
        stable_budget.get("unknown_quota_window_capacity"),
        stable_budget.get("unknown_lifetime_capacity"),
        default="DENY",
    )
    unknown_policy = str(stable_unknown or "DENY").strip().upper()
    if authority_is_current:
        authority_unknown = _first(
            current_budget.get("unknown_quota_window_capacity"),
            current_budget.get("unknown_lifetime_capacity"),
        )
        if authority_unknown is not None:
            unknown_policy = str(authority_unknown or "").strip().upper()
            unknown_source = (
                "governed_authority.task_budget.unknown_quota_window_capacity"
                if current_budget.get("unknown_quota_window_capacity") is not None
                else "governed_authority.task_budget.unknown_lifetime_capacity[compat]"
            )

    necessity_source = "adapter.default_denied"
    necessary_generation_authorized = False
    if authority_is_current:
        explicit = _first(
            generation_policy.get("necessary_generation_authorized"),
            generation_policy.get("necessity_based_new_generation_authorized"),
            current_budget.get("necessity_based_new_generation_authorized"),
        )
        necessary_generation_authorized = bool(explicit)
        necessity_source = "governed_authority.generation_policy"

    effect_source = "adapter.default_denied"
    generation_effect_authorized = False
    if authority_is_current:
        explicit_effect = _first(
            generation_policy.get("generation_effect_authorized"),
            generation_policy.get("automatic_next_generation_authorized"),
            generation_policy.get("provider_generation_authorized"),
        )
        generation_effect_authorized = bool(
            explicit_effect if explicit_effect is not None else necessary_generation_authorized
        )
        effect_source = "governed_authority.generation_policy"

    window_known_raw = _first(
        provider.get("quota_window_consumption_known"),
        provider.get("lifetime_consumption_known"),
        default=False,
    )
    window_known = bool(window_known_raw)
    proven_window_used = _first(
        provider.get("proven_quota_window_used"),
        provider.get("proven_lifetime_used"),
    )
    current_window_enumerated = _first(
        provider.get("current_window_enumerated_tasks"),
        provider.get("current_enumerated_tasks"),
    )
    direct_hard_limit = bool(
        provider.get("hard_ceiling_reached")
        or provider.get("hard_provider_limit_reached")
        or provider.get("quota_rejected")
    )

    hard_reserve = reserve_target if reserve_is_hard else 0
    budget = evaluate_task_budget(
        project=project,
        ceiling=ceiling,
        reserve=hard_reserve,
        quota_window_consumption_known=window_known,
        proven_quota_window_used=proven_window_used,
        current_window_enumerated_tasks=current_window_enumerated,
        unknown_quota_window_policy=unknown_policy,
        hard_ceiling_reached=direct_hard_limit,
    )

    state_blocks_effect = bool(
        state.get("unknown_write_state")
        or state.get("action_in_flight")
        or str(state.get("operation_state") or "").upper() in {"IN_FLIGHT", "UNKNOWN"}
    )
    if state_blocks_effect:
        generation_effect_authorized = False
        effect_source = "state_store.effect_reconciliation_required"

    provenance = {
        "precedence": [
            "adapter_stable_defaults",
            "governed_current_project_authority",
            "direct_provider_observation",
            "durable_state_store_effect_state",
        ],
        "authority_current": authority_is_current,
        "authority_event_id": authority_event_id,
        "ceiling": ceiling_source,
        "ceiling_resolved": ceiling_resolved,
        "reserve": reserve_source,
        "budget_basis": "CURRENT_QUOTA_WINDOW",
        "historical_usage_affects_capacity": False,
        "unknown_quota_window_policy": unknown_source,
        # Compatibility provenance key retained for existing consumers.
        "unknown_lifetime_policy": unknown_source,
        "necessary_generation_authorized": necessity_source,
        "generation_effect_authorized": effect_source,
        "provider_hard_limit_evidence": bool(direct_hard_limit),
        "state_store_effect_block": state_blocks_effect,
        "adapter_mutable_snapshot_is_authority": False,
    }

    return ResolvedExecutionPolicy(
        project=project,
        route=route,
        authority_event_id=authority_event_id,
        ceiling=ceiling,
        reserve_target=reserve_target,
        reserve_is_hard=reserve_is_hard,
        unknown_quota_window_policy=unknown_policy,
        necessary_generation_authorized=necessary_generation_authorized,
        generation_effect_authorized=generation_effect_authorized,
        budget=budget,
        provenance=provenance,
    )
