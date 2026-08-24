from __future__ import annotations

from typing import Any

UNKNOWN_LIFETIME_POLICIES = {
    "DENY",
    "ALLOW_UNLESS_DIRECT_CEILING_REACHED",
}


def evaluate_task_budget(
    *,
    project: str,
    ceiling: int,
    reserve: int,
    lifetime_consumption_known: bool,
    proven_lifetime_used: int | None,
    current_enumerated_tasks: int | None = None,
    unknown_lifetime_policy: str = "DENY",
    hard_ceiling_reached: bool = False,
) -> dict[str, Any]:
    """Evaluate project task capacity without making unknown history universal policy.

    When project authority explicitly selects
    `ALLOW_UNLESS_DIRECT_CEILING_REACHED`, unknown lifetime history alone does
    not freeze creation. Current enumeration is a direct lower bound only; it
    never claims to prove complete lifetime consumption. A directly observed
    ceiling/reserve boundary always fails closed.
    """

    if ceiling < 0 or reserve < 0:
        raise ValueError("ceiling and reserve must be non-negative")
    if reserve > ceiling:
        raise ValueError("reserve cannot exceed ceiling")
    if current_enumerated_tasks is not None and current_enumerated_tasks < 0:
        raise ValueError("current_enumerated_tasks must be non-negative")
    if proven_lifetime_used is not None and proven_lifetime_used < 0:
        raise ValueError("proven_lifetime_used must be non-negative")

    policy = str(unknown_lifetime_policy or "DENY").upper()
    if policy not in UNKNOWN_LIFETIME_POLICIES:
        raise ValueError(f"unknown unknown_lifetime_policy: {policy}")

    observed_lower_bound = max(
        int(current_enumerated_tasks or 0),
        int(proven_lifetime_used or 0),
    )
    effective_limit = max(0, ceiling - reserve)
    direct_limit_reached = bool(hard_ceiling_reached or observed_lower_bound >= effective_limit)

    common = {
        "schema_version": "2.1",
        "project": project,
        "ceiling": ceiling,
        "reserve": reserve,
        "proven_lifetime_used": proven_lifetime_used,
        "current_enumerated_tasks": current_enumerated_tasks,
        "observed_used_lower_bound": observed_lower_bound,
        "new_task_creation_authority": "PARENT_ONLY",
        "current_enumeration_proves_lifetime_consumption": False,
        "unknown_lifetime_policy": policy,
        "hard_ceiling_reached": direct_limit_reached,
    }

    if direct_limit_reached:
        return {
            **common,
            "state": "DIRECT_CEILING_OR_RESERVE_BOUNDARY_REACHED",
            "safe_remaining": 0 if lifetime_consumption_known else None,
            "observed_headroom": 0,
            "budget_allows_new_task": False,
            "automatic_new_task_creation": False,
            "fail_closed": True,
        }

    if not lifetime_consumption_known:
        if policy == "DENY":
            return {
                **common,
                "state": "UNKNOWN_LIFETIME_CONSUMPTION",
                "safe_remaining": None,
                "observed_headroom": max(0, effective_limit - observed_lower_bound),
                "budget_allows_new_task": False,
                "automatic_new_task_creation": False,
                "fail_closed": True,
            }

        return {
            **common,
            "state": "OWNER_POLICY_CAPACITY_AVAILABLE_WITH_UNKNOWN_LIFETIME",
            "safe_remaining": None,
            "observed_headroom": max(0, effective_limit - observed_lower_bound),
            "budget_allows_new_task": True,
            "automatic_new_task_creation": False,
            "fail_closed": False,
        }

    if proven_lifetime_used is None:
        raise ValueError("proven_lifetime_used is required when lifetime_consumption_known is true")

    if current_enumerated_tasks is not None and current_enumerated_tasks > proven_lifetime_used:
        return {
            **common,
            "state": "TASK_BUDGET_EVIDENCE_INCONSISTENT",
            "safe_remaining": None,
            "observed_headroom": None,
            "budget_allows_new_task": False,
            "automatic_new_task_creation": False,
            "fail_closed": True,
        }

    safe_remaining = max(0, effective_limit - proven_lifetime_used)
    state = "CAPACITY_AVAILABLE" if safe_remaining > 0 else "RESERVE_OR_CEILING_REACHED"
    return {
        **common,
        "state": state,
        "safe_remaining": safe_remaining,
        "observed_headroom": safe_remaining,
        "budget_allows_new_task": safe_remaining > 0,
        "automatic_new_task_creation": False,
        "fail_closed": safe_remaining <= 0,
    }


def evaluate_new_task_gate(
    budget: dict[str, Any],
    *,
    parent_gate_satisfied: bool,
    automatic_creation_authorized: bool = False,
) -> dict[str, Any]:
    budget_allows = bool(budget.get("budget_allows_new_task"))
    allowed = budget_allows and parent_gate_satisfied
    failures: list[str] = []
    if not budget_allows:
        failures.append("TASK_BUDGET_NOT_PROVEN_AVAILABLE")
    if not parent_gate_satisfied:
        failures.append("PARENT_GATE_REQUIRED")
    return {
        "schema_version": "2.1",
        "allowed": allowed,
        "authority": "PARENT_ONLY",
        "automatic_creation": bool(allowed and automatic_creation_authorized),
        "safe_remaining": budget.get("safe_remaining"),
        "observed_headroom": budget.get("observed_headroom"),
        "budget_state": budget.get("state"),
        "failures": failures,
    }
