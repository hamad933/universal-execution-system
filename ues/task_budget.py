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
    """Evaluate task capacity without turning unknown history into universal policy.

    `proven_lifetime_used` may be a proven lower bound when lifetime history is not
    enumerable. Projects may explicitly choose the non-blocking unknown-history
    policy, but a directly proven hard ceiling always wins and fails closed.
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

    common = {
        "schema_version": "2.1",
        "project": project,
        "ceiling": ceiling,
        "reserve": reserve,
        "current_enumerated_tasks": current_enumerated_tasks,
        "new_task_creation_authority": "PARENT_ONLY",
        "current_enumeration_proves_lifetime_consumption": False,
        "unknown_lifetime_policy": policy,
        "hard_ceiling_reached": bool(hard_ceiling_reached),
    }

    if hard_ceiling_reached:
        return {
            **common,
            "state": "DIRECT_HARD_CEILING_REACHED",
            "proven_lifetime_used": proven_lifetime_used,
            "safe_remaining": 0,
            "proven_floor_remaining": 0,
            "budget_allows_new_task": False,
            "automatic_new_task_creation": False,
            "fail_closed": True,
        }

    if not lifetime_consumption_known:
        if policy == "DENY":
            return {
                **common,
                "state": "UNKNOWN_LIFETIME_CONSUMPTION",
                "proven_lifetime_used": proven_lifetime_used,
                "safe_remaining": None,
                "proven_floor_remaining": None,
                "budget_allows_new_task": False,
                "automatic_new_task_creation": False,
                "fail_closed": True,
            }

        if proven_lifetime_used is None:
            return {
                **common,
                "state": "UNKNOWN_LIFETIME_WITHOUT_PROVEN_FLOOR",
                "proven_lifetime_used": None,
                "safe_remaining": None,
                "proven_floor_remaining": None,
                "budget_allows_new_task": False,
                "automatic_new_task_creation": False,
                "fail_closed": True,
            }

        if current_enumerated_tasks is not None and current_enumerated_tasks > proven_lifetime_used:
            return {
                **common,
                "state": "TASK_BUDGET_EVIDENCE_INCONSISTENT",
                "proven_lifetime_used": proven_lifetime_used,
                "safe_remaining": None,
                "proven_floor_remaining": None,
                "budget_allows_new_task": False,
                "automatic_new_task_creation": False,
                "fail_closed": True,
            }

        proven_floor_remaining = max(0, ceiling - reserve - proven_lifetime_used)
        allows = proven_floor_remaining > 0
        return {
            **common,
            "state": (
                "OWNER_POLICY_CAPACITY_AVAILABLE_WITH_UNKNOWN_LIFETIME"
                if allows
                else "RESERVE_OR_CEILING_REACHED"
            ),
            "proven_lifetime_used": proven_lifetime_used,
            "safe_remaining": None,
            "proven_floor_remaining": proven_floor_remaining,
            "budget_allows_new_task": allows,
            "automatic_new_task_creation": False,
            "fail_closed": not allows,
        }

    if proven_lifetime_used is None:
        raise ValueError("proven_lifetime_used is required when lifetime_consumption_known is true")

    if current_enumerated_tasks is not None and current_enumerated_tasks > proven_lifetime_used:
        return {
            **common,
            "state": "TASK_BUDGET_EVIDENCE_INCONSISTENT",
            "proven_lifetime_used": proven_lifetime_used,
            "safe_remaining": None,
            "proven_floor_remaining": None,
            "budget_allows_new_task": False,
            "automatic_new_task_creation": False,
            "fail_closed": True,
        }

    safe_remaining = max(0, ceiling - reserve - proven_lifetime_used)
    state = "CAPACITY_AVAILABLE" if safe_remaining > 0 else "RESERVE_OR_CEILING_REACHED"
    return {
        **common,
        "state": state,
        "proven_lifetime_used": proven_lifetime_used,
        "safe_remaining": safe_remaining,
        "proven_floor_remaining": safe_remaining,
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
        "proven_floor_remaining": budget.get("proven_floor_remaining"),
        "budget_state": budget.get("state"),
        "failures": failures,
    }
