from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


VALID_REPLACEMENT_CAUSES = frozenset(
    {
        "TERMINAL_WITH_REMAINING_WORK",
        "CONTINUATION_UNSUPPORTED",
        "CONTEXT_EXHAUSTED",
        "SESSION_CORRUPTED",
        "IRRECOVERABLY_INVALID_BINDING",
        "REPEATED_PROVEN_INEFFECTIVENESS",
        "FINAL_ASSURANCE_AUTHORIZED",
        "STALE_REVIEW_REQUIRES_CURRENT_SHA_REREVIEW",
        "STRUCTURED_HANDOFF_RECOVERY_REQUIRED",
        "OTHER_GOVERNED_REPLACEMENT_CAUSE",
    }
)
SUPPORTED_GENERATION_ROLES = frozenset({"WRITER", "REVIEWER", "ASSURANCE", "FINAL_ASSURANCE"})


def _canonical_digest(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def generation_transition_key(
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    current_generation: int,
    predecessor_session_fingerprint: str | None,
    candidate_sha: str | None,
    replacement_cause: str,
) -> str:
    payload = {
        "project": str(project).strip(),
        "route": str(route).strip(),
        "workstream": str(workstream).strip(),
        "role": str(role).strip().upper(),
        "current_generation": int(current_generation),
        "predecessor_session_fingerprint": predecessor_session_fingerprint or "NONE",
        "candidate_sha": str(candidate_sha or "").strip().lower() or "NONE",
        "replacement_cause": str(replacement_cause).strip().upper(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def initial_lineage_transition_key(
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    candidate_sha: str | None,
    initial_task_spec: Mapping[str, Any],
) -> str:
    payload = {
        "project": str(project).strip(),
        "route": str(route).strip(),
        "workstream": str(workstream).strip(),
        "role": str(role).strip().upper(),
        "creation_kind": "INITIAL_LOGICAL_LINEAGE",
        "current_generation": 0,
        "predecessor_session_fingerprint": "NONE",
        "candidate_sha": str(candidate_sha or "").strip().lower() or "NONE",
        "initial_task_spec_digest": _canonical_digest(initial_task_spec),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def assess_initial_lineage_creation(
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    current_generation: int,
    predecessor_session_fingerprint: str | None,
    candidate_sha: str | None,
    current_policy: Mapping[str, Any],
    active_duplicate_absent: bool,
    unknown_write_state: bool,
    effect_in_flight: bool,
    exact_repository_binding: bool,
    exact_starting_ref_binding: bool,
    initial_task_spec: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Assess creation of the first physical generation for a new logical lineage.

    This is intentionally separate from replacement assessment. It never treats
    an absent/unbound session as a replacement cause and performs no provider
    effect. A later executor may consume an allowed assessment only together
    with fresh explicit Parent initial-lineage authority.
    """

    role_name = str(role or "").strip().upper()
    failures: list[str] = []
    task_spec = dict(initial_task_spec) if isinstance(initial_task_spec, Mapping) else {}

    if role_name not in SUPPORTED_GENERATION_ROLES:
        failures.append("ROLE_NOT_SUPPORTED")
    if int(current_generation) != 0 or str(predecessor_session_fingerprint or "").strip():
        failures.append("INITIAL_LINEAGE_ALREADY_EXISTS")
    if not bool(current_policy.get("necessary_generation_authorized")):
        failures.append("CURRENT_PROJECT_AUTHORITY_REQUIRED")
    if not bool(current_policy.get("generation_effect_authorized")):
        failures.append("PROVIDER_GENERATION_EFFECT_NOT_AUTHORIZED")
    if not bool(current_policy.get("generation_budget_safe")):
        failures.append("TASK_BUDGET_NOT_PROVEN_AVAILABLE")
    if bool(current_policy.get("budget", {}).get("hard_ceiling_reached")):
        failures.append("DIRECT_HARD_CEILING_REACHED")
    if unknown_write_state:
        failures.append("UNKNOWN_WRITE_RECONCILIATION_REQUIRED")
    if effect_in_flight:
        failures.append("EFFECT_IN_FLIGHT_RECONCILIATION_REQUIRED")
    if not active_duplicate_absent:
        failures.append("ACTIVE_DUPLICATE_CHECK_REQUIRED")
    if not exact_repository_binding:
        failures.append("EXACT_REPOSITORY_BINDING_REQUIRED")
    if not exact_starting_ref_binding:
        failures.append("EXACT_STARTING_REF_BINDING_REQUIRED")
    if not task_spec:
        failures.append("INITIAL_TASK_SPEC_REQUIRED")

    transition_key = (
        initial_lineage_transition_key(
            project=project,
            route=route,
            workstream=workstream,
            role=role_name,
            candidate_sha=candidate_sha,
            initial_task_spec=task_spec,
        )
        if task_spec
        else None
    )
    return {
        "schema_version": "1.0",
        "allowed": not failures,
        "failures": failures,
        "transition_key": transition_key,
        "creation_kind": "INITIAL_LOGICAL_LINEAGE",
        "current_generation": 0,
        "next_generation": 1,
        "initial_logical_lineage": True,
        "minimum_generation_count": 1 if not failures else 0,
        "safe_to_blind_retry": False,
    }


def assess_generation_transition(
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    current_generation: int,
    predecessor_session_fingerprint: str | None,
    candidate_sha: str | None,
    replacement_cause: str,
    work_remaining: bool,
    current_policy: Mapping[str, Any],
    active_duplicate_absent: bool,
    unknown_write_state: bool,
    exact_repository_binding: bool,
    exact_starting_ref_binding: bool,
    replacement_task_spec_ready: bool,
) -> dict[str, Any]:
    """Evaluate one next-physical-generation transition without performing it."""

    role_name = str(role or "").strip().upper()
    cause = str(replacement_cause or "").strip().upper()
    failures: list[str] = []

    if role_name not in SUPPORTED_GENERATION_ROLES:
        failures.append("ROLE_NOT_SUPPORTED")
    if cause not in VALID_REPLACEMENT_CAUSES:
        failures.append("REPLACEMENT_CAUSE_NOT_GOVERNED")
    # A stale exact-SHA review is itself remaining review work: candidate movement
    # invalidates the prior evidence even when the implementation PR is already
    # closed/merged. Do not make current-SHA rereview depend on PR-open topology.
    if not work_remaining and cause not in {
        "FINAL_ASSURANCE_AUTHORIZED",
        "STRUCTURED_HANDOFF_RECOVERY_REQUIRED",
        "STALE_REVIEW_REQUIRES_CURRENT_SHA_REREVIEW",
    }:
        failures.append("NO_REMAINING_WORK")
    if cause == "STRUCTURED_HANDOFF_RECOVERY_REQUIRED" and role_name not in {
        "REVIEWER",
        "ASSURANCE",
        "FINAL_ASSURANCE",
    }:
        failures.append("STRUCTURED_HANDOFF_RECOVERY_REQUIRES_REVIEW_ROLE")
    if not bool(current_policy.get("necessary_generation_authorized")):
        failures.append("CURRENT_PROJECT_AUTHORITY_REQUIRED")
    if not bool(current_policy.get("generation_effect_authorized")):
        failures.append("PROVIDER_GENERATION_EFFECT_NOT_AUTHORIZED")
    if not bool(current_policy.get("generation_budget_safe")):
        failures.append("TASK_BUDGET_NOT_PROVEN_AVAILABLE")
    if bool(current_policy.get("budget", {}).get("hard_ceiling_reached")):
        failures.append("DIRECT_HARD_CEILING_REACHED")
    if unknown_write_state:
        failures.append("UNKNOWN_WRITE_RECONCILIATION_REQUIRED")
    if not active_duplicate_absent:
        failures.append("ACTIVE_DUPLICATE_CHECK_REQUIRED")
    if not exact_repository_binding:
        failures.append("EXACT_REPOSITORY_BINDING_REQUIRED")
    if not exact_starting_ref_binding:
        failures.append("EXACT_STARTING_REF_BINDING_REQUIRED")
    if not replacement_task_spec_ready:
        failures.append("EXACT_REPLACEMENT_TASK_SPEC_REQUIRED")

    transition_key = generation_transition_key(
        project=project,
        route=route,
        workstream=workstream,
        role=role_name,
        current_generation=current_generation,
        predecessor_session_fingerprint=predecessor_session_fingerprint,
        candidate_sha=candidate_sha,
        replacement_cause=cause,
    )
    return {
        "schema_version": "1.0",
        "allowed": not failures,
        "failures": failures,
        "transition_key": transition_key,
        "current_generation": int(current_generation),
        "next_generation": int(current_generation) + 1,
        "replacement_cause": cause,
        "same_logical_lineage": True,
        "minimum_generation_count": 1 if not failures else 0,
        "safe_to_blind_retry": False,
    }
