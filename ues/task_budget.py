from __future__ import annotations

from typing import Any


def evaluate_task_budget(
    *,
    project: str,
    ceiling: int,
    reserve: int,
    lifetime_consumption_known: bool,
    proven_lifetime_used: int | None,
    current_enumerated_tasks: int | None = None,
) -> dict[str, Any]:
    if ceiling < 0 or reserve < 0:
        raise ValueError("ceiling and reserve must be non-negative")
    if reserve > ceiling:
        raise ValueError("reserve cannot exceed ceiling")
    if current_enumerated_tasks is not None and current_enumerated_tasks < 0:
        raise ValueError("current_enumerated_tasks must be non-negative")

    if not lifetime_consumption_known or proven_lifetime_used is None:
        return {
            "schema_version": "2.0",
            "project": project,
            "state": "UNKNOWN_LIFETIME_CONSUMPTION",
            "ceiling": ceiling,
            "reserve": reserve,
            "proven_lifetime_used": None,
            "current_enumerated_tasks": current_enumerated_tasks,
            "safe_remaining": None,
            "budget_allows_new_task": False,
            "new_task_creation_authority": "PARENT_ONLY",
            "automatic_new_task_creation": False,
            "fail_closed": True,
            "current_enumeration_proves_lifetime_consumption": False,
        }

    if proven_lifetime_used < 0:
        raise ValueError("proven_lifetime_used must be non-negative")
    if current_enumerated_tasks is not None and current_enumerated_tasks > proven_lifetime_used:
        return {
            "schema_version": "2.0",
            "project": project,
            "state": "TASK_BUDGET_EVIDENCE_INCONSISTENT",
            "ceiling": ceiling,
            "reserve": reserve,
            "proven_lifetime_used": proven_lifetime_used,
            "current_enumerated_tasks": current_enumerated_tasks,
            "safe_remaining": None,
            "budget_allows_new_task": False,
            "new_task_creation_authority": "PARENT_ONLY",
            "automatic_new_task_creation": False,
            "fail_closed": True,
            "current_enumeration_proves_lifetime_consumption": False,
        }

    safe_remaining = max(0, ceiling - reserve - proven_lifetime_used)
    state = "CAPACITY_AVAILABLE" if safe_remaining > 0 else "RESERVE_OR_CEILING_REACHED"
    return {
        "schema_version": "2.0",
        "project": project,
        "state": state,
        "ceiling": ceiling,
        "reserve": reserve,
        "proven_lifetime_used": proven_lifetime_used,
        "current_enumerated_tasks": current_enumerated_tasks,
        "safe_remaining": safe_remaining,
        "budget_allows_new_task": safe_remaining > 0,
        "new_task_creation_authority": "PARENT_ONLY",
        "automatic_new_task_creation": False,
        "fail_closed": safe_remaining <= 0,
        "current_enumeration_proves_lifetime_consumption": False,
    }


def evaluate_new_task_gate(
    budget: dict[str, Any],
    *,
    parent_gate_satisfied: bool,
) -> dict[str, Any]:
    budget_allows = bool(budget.get("budget_allows_new_task"))
    allowed = budget_allows and parent_gate_satisfied
    failures: list[str] = []
    if not budget_allows:
        failures.append("TASK_BUDGET_NOT_PROVEN_AVAILABLE")
    if not parent_gate_satisfied:
        failures.append("PARENT_GATE_REQUIRED")
    return {
        "schema_version": "2.0",
        "allowed": allowed,
        "authority": "PARENT_ONLY",
        "automatic_creation": False,
        "safe_remaining": budget.get("safe_remaining"),
        "budget_state": budget.get("state"),
        "failures": failures,
    }
