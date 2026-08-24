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
    ceiling: int
    reserve_target: int
    reserve_is_hard: bool
    unknown_lifetime_policy: str
    necessary_generation_authorized: bool
    generation_effect_authorized: bool
    budget: Mapping[str, Any]
    provenance: Mapping[str, Any]

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
            "schema_version": "1.0",
            "project": self.project,
            "route": self.route,
            "authority_event_id": self.authority_event_id,
            "ceiling": self.ceiling,
            "reserve_target": self.reserve_target,
            "reserve_is_hard": self.reserve_is_hard,
            "unknown_lifetime_policy": self.unknown_lifetime_policy,
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

    Precedence is intentionally asymmetric:
    stable adapter defaults < current governed project authority < direct provider
    observation < durable StateStore effect state.

    Adapter values are never interpreted as proof of a current Owner decision.
    A governed authority payload may override mutable ceilings, reserve semantics,
    unknown-history policy and necessity-based generation authorization without
    editing the committed adapter. Provider hard-limit evidence always wins.
    StateStore UNKNOWN/in-flight state never grants a new effect.
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

    ceiling_source = "adapter.default"
    ceiling_value = stable_budget.get("ceiling")
    if authority_is_current and current_budget.get("ceiling") is not None:
        ceiling_value = current_budget.get("ceiling")
        ceiling_source = "governed_authority.task_budget.ceiling"
    ceiling = _integer(ceiling_value, "ceiling")

    reserve_source = "adapter.default"
    reserve_value = _first(stable_budget.get("reserve_target"), stable_budget.get("reserve"), default=0)
    if authority_is_current and (
        current_budget.get("reserve_target") is not None or current_budget.get("reserve") is not None
    ):
        reserve_value = _first(current_budget.get("reserve_target"), current_budget.get("reserve"), default=0)
        reserve_source = "governed_authority.task_budget.reserve"
    reserve_target = _integer(reserve_value, "reserve_target")
    if reserve_target > ceiling:
        raise PolicyResolutionError("reserve_target cannot exceed ceiling")

    reserve_is_hard = bool(
        _first(
            current_budget.get("reserve_is_hard") if authority_is_current else None,
            stable_budget.get("reserve_is_hard"),
            default=False,
        )
    )

    unknown_source = "adapter.default"
    unknown_policy = str(stable_budget.get("unknown_lifetime_capacity") or "DENY").strip().upper()
    if authority_is_current and current_budget.get("unknown_lifetime_capacity") is not None:
        unknown_policy = str(current_budget.get("unknown_lifetime_capacity") or "").strip().upper()
        unknown_source = "governed_authority.task_budget.unknown_lifetime_capacity"

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
        # A current authority that explicitly authorizes a necessary generation
        # is sufficient for the guarded runtime effect; it does not enable any
        # unrelated mutation and still requires every binding/idempotency guard.
        generation_effect_authorized = bool(
            explicit_effect if explicit_effect is not None else necessary_generation_authorized
        )
        effect_source = "governed_authority.generation_policy"

    lifetime_known = bool(provider.get("lifetime_consumption_known", False))
    proven_lifetime_used = provider.get("proven_lifetime_used")
    current_enumerated = provider.get("current_enumerated_tasks")
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
        lifetime_consumption_known=lifetime_known,
        proven_lifetime_used=proven_lifetime_used,
        current_enumerated_tasks=current_enumerated,
        unknown_lifetime_policy=unknown_policy,
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
        "reserve": reserve_source,
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
        unknown_lifetime_policy=unknown_policy,
        necessary_generation_authorized=necessary_generation_authorized,
        generation_effect_authorized=generation_effect_authorized,
        budget=budget,
        provenance=provenance,
    )
