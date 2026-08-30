from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from . import lifecycle_runtime as legacy
from .current_authority import load_current_authority_json
from .generation_transition import initial_lineage_transition_key
from .evidence_supplement_runtime import evidence_supplement_entries, run_evidence_supplements
from .initial_lineage_effects import execute_initial_lineage_generation
from .initial_lineage_reconciliation import reconcile_unknown_initial_lineage
from .jules_lifecycle import JulesLifecycleClient
from .lineage_registry import lineage_lane_id
from .live_runtime import build_live_state_store
from .policy_resolution import resolve_execution_policy
from .providers.base import NetworkError, RateLimitError, ServerError
from .providers.github import GitHubClient
from .structured_handoff import build_required_handoff_instructions
from .task_budget import observe_rolling_quota_window

SCHEMA_VERSION = "1.0"
SUPPORTED_PROJECTS = frozenset({"GS", "CEP", "RP01", "RP02", "RP03", "RP04"})
SUPPORTED_ROLES = frozenset({"WRITER", "REVIEWER", "ASSURANCE", "FINAL_ASSURANCE"})
JULES_TASK_QUOTA_WINDOW_SECONDS = 24 * 60 * 60
_PRE_EFFECT_PROVIDER_READ_OPERATIONS = frozenset({"jules.sessions.list", "jules.sessions.get"})
_PRE_EFFECT_PROVIDER_READ_ERRORS = (NetworkError, RateLimitError, ServerError)
_PROVIDER_READ_UNAVAILABLE_RESULT = "INITIAL_LINEAGE_PROVIDER_READ_UNAVAILABLE_BEFORE_EFFECTS"
_PROVIDER_READ_UNAVAILABLE_EXIT = 75
_DEFAULT_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS = 2
_MAX_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS = 3
_PROVIDER_INVENTORY_ATTEMPTS_ENV = "UES_INITIAL_LINEAGE_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS"
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_REF = re.compile(r"^[A-Za-z0-9._/-]+$")
_ALLOWED_TASK_FIELDS = frozenset(
    {
        "objective",
        "exact_baseline",
        "write_scope",
        "writeScope",
        "prohibited_scope",
        "prohibitedScope",
        "validation",
        "tests",
        "evidence",
        "handoff",
        "stop_gate",
        "stopGate",
    }
)


def _load_adapter(project: str) -> dict[str, Any]:
    name = str(project or "").strip().upper()
    if name not in SUPPORTED_PROJECTS:
        raise ValueError("unsupported project for initial lineage runtime")
    path = Path(__file__).resolve().parents[1] / "adapters" / f"{name.lower()}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or str(value.get("project") or "").upper() != name:
        raise ValueError("project adapter identity mismatch")
    if value.get("activation", {}).get("default_mode") != "SHADOW":
        raise ValueError("initial lineage runtime requires SHADOW-default adapter")
    return value


def _state_role(role: str) -> str:
    return "ASSURANCE" if str(role).upper() == "FINAL_ASSURANCE" else str(role).upper()


def _role_key(role: str) -> str:
    return {
        "WRITER": "writer",
        "REVIEWER": "reviewer",
        "ASSURANCE": "assurance",
        "FINAL_ASSURANCE": "final_assurance",
    }[str(role).upper()]


def _parse_lane_key(value: str) -> tuple[str, str]:
    workstream, sep, role = str(value or "").rpartition(":")
    role = role.upper()
    if not sep or not workstream.strip() or role not in SUPPORTED_ROLES:
        raise ValueError("initial lineage authority key must be <workstream>:<supported-role>")
    return workstream.strip(), role


def _dynamic_role_config(
    authority: Mapping[str, Any], *, workstream: str, role: str
) -> Mapping[str, Any] | None:
    lineages = authority.get("lineages")
    lineages = lineages if isinstance(lineages, Mapping) else {}
    config = lineages.get(workstream)
    if not isinstance(config, Mapping):
        return None
    role_config = config.get(_role_key(role))
    return role_config if isinstance(role_config, Mapping) else None


def _single_task_key(task_spec: Mapping[str, Any], *keys: str) -> str:
    present = [key for key in keys if key in task_spec]
    if not present:
        raise ValueError(f"task_spec.{keys[0]} is required")
    if len(present) != 1:
        raise ValueError(f"task_spec aliases are ambiguous: {', '.join(present)}")
    return present[0]


def _string_list(task_spec: Mapping[str, Any], *keys: str, required_nonempty: bool = False) -> list[str]:
    selected = _single_task_key(task_spec, *keys)
    value = task_spec.get(selected)
    if not isinstance(value, list):
        raise ValueError(f"task_spec.{selected} must be a list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"task_spec.{selected} must contain only non-empty strings")
    items = [item.strip() for item in value]
    if required_nonempty and not items:
        raise ValueError(f"task_spec.{selected} must not be empty")
    return items


def _required_text(task_spec: Mapping[str, Any], *keys: str) -> str:
    selected = _single_task_key(task_spec, *keys)
    value = task_spec.get(selected)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"task_spec.{selected} must be a non-empty string")
    return value.strip()


def _validate_task_spec(task_spec: Mapping[str, Any], *, role: str) -> dict[str, Any]:
    """Validate the complete bounded executor contract before any provider write.

    The Current Authority task specification is the sole scope source for the
    first physical generation. The schema is closed: unknown fields, conflicting
    aliases, and non-string scope entries are rejected rather than being copied
    into the provider prompt. Writers require a non-empty write domain;
    reviewer/assurance roles are explicitly read-only.
    """

    role_name = str(role or "").strip().upper()
    if role_name not in SUPPORTED_ROLES:
        raise ValueError("task_spec role is not supported")
    unknown = sorted(str(key) for key in task_spec if key not in _ALLOWED_TASK_FIELDS)
    if unknown:
        raise ValueError("task_spec contains unsupported fields: " + ", ".join(unknown))

    result = dict(task_spec)
    _required_text(task_spec, "objective")
    _required_text(task_spec, "exact_baseline")
    write_scope = _string_list(task_spec, "write_scope", "writeScope")
    _string_list(task_spec, "prohibited_scope", "prohibitedScope")
    _string_list(task_spec, "validation", "tests", required_nonempty=True)
    _string_list(task_spec, "evidence", required_nonempty=True)
    _required_text(task_spec, "handoff")
    _required_text(task_spec, "stop_gate", "stopGate")

    if role_name == "WRITER" and not write_scope:
        raise ValueError("Writer task_spec.write_scope must not be empty")
    if role_name in {"REVIEWER", "ASSURANCE", "FINAL_ASSURANCE"} and write_scope:
        raise ValueError("Reviewer/Assurance task_spec.write_scope must be empty")
    return result


def _dynamic_provider_starting_branch(role_config: Mapping[str, Any]) -> str:
    branch = role_config.get("provider_starting_branch")
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("dynamic lineage role must declare provider_starting_branch")
    return branch.strip()


def _parse_exact_baseline(task_spec: Mapping[str, Any]) -> tuple[str, str]:
    raw = str(task_spec.get("exact_baseline") or "").strip()
    ref, sep, sha = raw.rpartition("@")
    ref = ref.strip()
    sha = sha.strip().lower()
    if ref.startswith("refs/heads/"):
        ref = ref[len("refs/heads/") :]
    elif ref.startswith("refs/"):
        raise ValueError("task_spec.exact_baseline must reference a branch, not another Git ref namespace")
    if (
        not sep
        or not ref
        or not _REF.fullmatch(ref)
        or ref.startswith("/")
        or ref.endswith("/")
        or ".." in ref
        or "@{" in ref
        or not _SHA.fullmatch(sha)
    ):
        raise ValueError("task_spec.exact_baseline must be an exact branch@40hex-sha binding")
    return ref, sha


def _task_prompt(
    task_spec: Mapping[str, Any],
    *,
    role: str | None = None,
    workstream: str | None = None,
) -> str:
    canonical = json.dumps(dict(task_spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    base = (
        "Execute exactly the following Parent-governed task specification. "
        "Do not widen scope or perform any action prohibited by it. Return the required evidence and stop at its stop_gate.\n\n"
        + canonical
    )
    if role is None and workstream is None:
        return base
    if role is None or workstream is None:
        raise ValueError("role and workstream are both required for machine-actionable handoff enforcement")

    state_role = _state_role(role)
    instructions = build_required_handoff_instructions(state_role, workstream)
    if state_role in {"REVIEWER", "ASSURANCE"}:
        _, candidate_sha = _parse_exact_baseline(task_spec)
        instructions = instructions.replace(
            '"candidate_sha": null', f'"candidate_sha": "{candidate_sha}"'
        ).replace(
            '"reviewed_sha": null', f'"reviewed_sha": "{candidate_sha}"'
        )
        instructions += (
            "\nFor this READ_ONLY review/assurance task, candidate_sha and reviewed_sha MUST both remain exactly "
            f"{candidate_sha}. If that exact SHA was not actually reviewed, do not claim a verdict; return a blocked or "
            "UNKNOWN handoff that states the evidence boundary instead."
        )
    return base + "\n\n" + instructions


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


def _state_snapshot(
    store: Any, *, project: str, route: str, workstream: str, role: str
) -> dict[str, Any]:
    lane_id = lineage_lane_id(project, route, workstream, _state_role(role))
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        return {"generation": 0, "session_fingerprint": None}
    evidence = read.record.evidence_bindings or {}
    return {
        "generation": int(evidence.get("generation") or 0),
        "session_fingerprint": str(evidence.get("session_fingerprint") or "").strip() or None,
        "unknown_write_state": read.record.unknown_write_state,
        "action_in_flight": read.record.action_in_flight,
        "operation_state": (read.record.operation_receipt or {}).get("state")
        if isinstance(read.record.operation_receipt, Mapping)
        else None,
        "pending_initial_lineage_transition": evidence.get("pending_initial_lineage_transition"),
    }


def _governed_initial_policy(
    authority: Mapping[str, Any], lane_authority: Mapping[str, Any]
) -> dict[str, Any]:
    governed = dict(authority)
    generation = (
        dict(authority.get("generation_policy") or {})
        if isinstance(authority.get("generation_policy"), Mapping)
        else {}
    )
    enabled = lane_authority.get("authorized") is True
    generation["necessary_generation_authorized"] = enabled
    generation["generation_effect_authorized"] = enabled
    governed["generation_policy"] = generation
    return governed


def _marker_matches(
    inventory: list[dict[str, Any]],
    *,
    repository: str,
    starting_branch: str,
    marker: str,
) -> list[dict[str, Any]]:
    token = f"[{marker}]"
    result: list[dict[str, Any]] = []
    for session in inventory:
        if str(session.get("_source_repository") or "").casefold() != repository.casefold():
            continue
        if str(session.get("sourceStartingBranch") or "") != starting_branch:
            continue
        title = str(session.get("title") or session.get("displayName") or "")
        if token in title and str(session.get("name") or "").strip():
            result.append(session)
    return result


def _authority_entries(authority: Mapping[str, Any]) -> Mapping[str,Any]:
    generation = authority.get("generation_policy")
    generation = generation if isinstance(generation, Mapping) else {}
    value = generation.get("authorized_initial_lineages")
    return value if isinstance(value, Mapping) else {}


def _is_pre_effect_provider_read_failure(exc: BaseException) -> bool:
    return isinstance(exc, _PRE_EFFECT_PROVIDER_READ_ERRORS) and str(
        getattr(exc, "operation", "") or ""
    ) in _PRE_EFFECT_PROVIDER_READ_OPERATIONS


def _provider_inventory_snapshot_attempts() -> int:
    raw = str(os.environ.get(_PROVIDER_INVENTORY_ATTEMPTS_ENV) or "").strip()
    if not raw:
        return _DEFAULT_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS
    try:
        configured = int(raw)
    except ValueError:
        return _DEFAULT_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS
    return max(1, min(configured, _MAX_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS))


def _provider_inventory_with_retry(
    client: object,
    attempt_limit: int | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    limit = _provider_inventory_snapshot_attempts() if attempt_limit is None else int(attempt_limit)
    limit = max(1, min(limit, _MAX_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS))
    attempts = 0
    while True:
        attempts += 1
        try:
            inventory = legacy._provider_inventory(client)
            return inventory, attempts, limit
        except _PRE_EFFECT_PROVIDER_READ_ERRORS as exc:
            if not _is_pre_effect_provider_read_failure(exc) or attempts >= limit:
                raise


def _provider_read_unavailable_result(
    project: str,
    route: str,
    *,
    authority: Mapping[str, Any],
    exc: BaseException,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "route": route,
        "result": _PROVIDER_READ_UNAVAILABLE_RESULT,
        "authority_event_id": authority.get("authority_event_id"),
        "provider_read_authoritative": False,
        "provider_read_operation": getattr(exc, "operation", None),
        "provider_read_error_category": getattr(exc, "category", "PROVIDER_READ_ERROR"),
        "provider_write_attempted": False,
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "retry_condition": "FRESH_AUTHORITATIVE_PROVIDER_READ_REQUIRED",
        "raw_session_ids_persisted": False,
        "safe_to_blind_retry": False,
    }


def run(project: str) -> dict[str, Any]:
    adapter = _load_adapter(project)
    project_id = str(adapter.get("project") or project.upper())
    route = str(adapter.get("route") or project_id)
    repository = str(adapter.get("repository") or "").strip()
    actor = str(os.environ.get("UES_AUTHORITY_TRANSPORT_ACTOR") or os.environ.get("GITHUB_ACTOR") or "").strip()
    authority = load_current_authority_json(
        adapter,
        os.environ.get("UES_CURRENT_AUTHORITY_JSON"),
        transport_actor=actor,
    )
    if authority is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "project": project_id,
            "route": route,
            "result": "INITIAL_LINEAGE_RUNTIME_NO_CURRENT_AUTHORITY",
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "safe_to_blind_retry": False,
        }

    entries = _authority_entries(authority)
    supplement_entries = evidence_supplement_entries(authority)
    if not entries and not supplement_entries:
        return {
            "schema_version": SCHEMA_VERSION,
            "project": project_id,
            "route": route,
            "result": "INITIAL_LINEAGE_RUNTIME_NO_AUTHORIZED_INITIAL_LINEAGES",
            "authority_event_id": authority.get("authority_event_id"),
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "safe_to_blind_retry": False,
        }

    key = str(os.environ.get("JULES_API_KEY") or "").strip()
    github_token = str(os.environ.get("GITHUB_TOKEN") or "").strip()
    if not key or not github_token:
        raise RuntimeError("JULES_API_KEY and GITHUB_TOKEN are required for authorized initial lineage runtime")

    store = build_live_state_store()
    jules = JulesLifecycleClient(key)
    github = GitHubClient(github_token)
    try:
        inventory, _, _ = _provider_inventory_with_retry(jules)
    except _PRE_EFFECT_PROVIDER_READ_ERRORS as exc:
        if not _is_pre_effect_provider_read_failure(exc):
            raise
        return _provider_read_unavailable_result(
            project_id,
            route,
            authority=authority,
            exc=exc,
        )
    source_name, source_proven = _source_for_repository(jules, repository) if entries else (None, False)
    # Jules currently meters tasks in a rolling 24-hour window. Historical
    # sessions stay in inventory for reconciliation/marker matching but are not
    # charged against the current capacity gate.
    provider_observation = observe_rolling_quota_window(
        inventory,
        window_seconds=JULES_TASK_QUOTA_WINDOW_SECONDS,
    )
    owner, repo = legacy._repo_parts(repository)
    event_id = str(authority.get("authority_event_id") or "").strip()

    results: list[dict[str, Any]] = []
    for raw_key, raw_lane in sorted(entries.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_lane, Mapping) or raw_lane.get("authorized") is not True:
            continue
        try:
            workstream, role = _parse_lane_key(str(raw_key))
        except ValueError as exc:
            results.append(
                {
                    "authority_key": str(raw_key),
                    "decision": "INITIAL_LINEAGE_AUTHORITY_KEY_INVALID",
                    "reason": str(exc),
                    "provider_write_attempted": False,
                    "safe_to_blind_retry": False,
                }
            )
            continue

        dynamic_role = _dynamic_role_config(authority, workstream=workstream, role=role)
        if dynamic_role is None:
            results.append(
                {
                    "workstream": workstream,
                    "role": role,
                    "decision": "INITIAL_LINEAGE_DYNAMIC_TOPOLOGY_REQUIRED",
                    "provider_write_attempted": False,
                    "safe_to_blind_retry": False,
                }
            )
            continue

        raw_task_spec = raw_lane.get("task_spec")
        if not isinstance(raw_task_spec, Mapping):
            results.append(
                {
                    "workstream": workstream,
                    "role": role,
                    "decision": "INITIAL_LINEAGE_TASK_SPEC_REQUIRED",
                    "provider_write_attempted": False,
                    "safe_to_blind_retry": False,
                }
            )
            continue
        try:
            task_spec = _validate_task_spec(raw_task_spec, role=role)
            starting_branch, candidate_sha = _parse_exact_baseline(task_spec)
            dynamic_branch = _dynamic_provider_starting_branch(dynamic_role)
            if dynamic_branch != starting_branch:
                raise ValueError("dynamic provider_starting_branch must match task_spec.exact_baseline branch")
        except ValueError as exc:
            results.append(
                {
                    "workstream": workstream,
                    "role": role,
                    "decision": "INITIAL_LINEAGE_TASK_CONTRACT_INVALID",
                    "reason": str(exc),
                    "provider_write_attempted": False,
                    "safe_to_blind_retry": False,
                }
            )
            continue

        state = _state_snapshot(
            store,
            project=project_id,
            route=route,
            workstream=workstream,
            role=role,
        )
        governed = _governed_initial_policy(authority, raw_lane)
        effective = resolve_execution_policy(
            adapter=adapter,
            governed_authority=governed,
            provider_observation=provider_observation,
            state_snapshot=state,
        ).to_dict()

        if state.get("unknown_write_state") and isinstance(
            state.get("pending_initial_lineage_transition"), Mapping
        ):
            reconciliation = reconcile_unknown_initial_lineage(
                store,
                project=project_id,
                route=route,
                workstream=workstream,
                role=role,
                inventory=inventory,
                authority_event_id=event_id,
                policy_provenance=effective.get("provenance")
                if isinstance(effective.get("provenance"), Mapping)
                else {},
            )
            results.append(
                {
                    "workstream": workstream,
                    "role": role,
                    "current_policy": effective,
                    "effect": reconciliation,
                }
            )
            continue

        exact = github.verify_exact_head(owner, repo, starting_branch, candidate_sha)
        exact_ref = bool(exact.get("exact_head_match"))
        transition_key = initial_lineage_transition_key(
            project=project_id,
            route=route,
            workstream=workstream,
            role=role,
            candidate_sha=candidate_sha,
            initial_task_spec=task_spec,
        )
        matches = _marker_matches(
            inventory,
            repository=repository,
            starting_branch=starting_branch,
            marker=transition_key[:12],
        )
        if int(state.get("generation") or 0) == 0 and matches:
            effect = {
                "decision": "INITIAL_LINEAGE_EXISTING_PROVIDER_MARKER_REQUIRES_ADJUDICATION",
                "provider_write_attempted": False,
                "match_count": len(matches),
                "transition_key": transition_key,
                "safe_to_blind_retry": False,
            }
        elif not source_name or not source_proven or not exact_ref:
            effect = {
                "decision": "INITIAL_LINEAGE_EXACT_SOURCE_OR_BASELINE_REQUIRED",
                "provider_write_attempted": False,
                "source_binding_proven": bool(source_name and source_proven),
                "exact_starting_ref_binding": exact_ref,
                "safe_to_blind_retry": False,
            }
        else:
            effect = execute_initial_lineage_generation(
                store,
                jules,
                adapter=adapter,
                authority=authority,
                transport_actor=actor,
                current_policy=effective,
                project=project_id,
                route=route,
                workstream=workstream,
                role=role,
                task_spec=task_spec,
                prompt=_task_prompt(task_spec, role=role, workstream=workstream),
                title=f"{project_id} {workstream} {role} G1",
                source_name=source_name,
                starting_branch=starting_branch,
                repository=repository,
                candidate_sha=candidate_sha,
                active_duplicate_absent=not matches,
                exact_repository_binding=source_proven,
                exact_starting_ref_binding=exact_ref,
            )
        results.append(
            {
                "workstream": workstream,
                "role": role,
                "candidate_sha": candidate_sha,
                "starting_branch": starting_branch,
                "dynamic_topology_bound": True,
                "current_policy": effective,
                "effect": effect,
            }
        )

    if supplement_entries:
        results.extend(
            run_evidence_supplements(
                adapter=adapter,
                authority=authority,
                entries=supplement_entries,
                store=store,
                jules=jules,
                github=github,
                inventory=inventory,
                provider_observation=provider_observation,
                actor=actor,
            )
        )

    decisions = Counter(
        str((item.get("effect") or item).get("decision") or "NO_EFFECT") for item in results
    )
    external = sum(int((item.get("effect") or {}).get("external_effects_dispatched") or 0) for item in results)
    created = sum(int((item.get("effect") or {}).get("new_tasks_or_sessions_created") or 0) for item in results)
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project_id,
        "route": route,
        "result": "INITIAL_LINEAGE_RUNTIME_COMPLETE",
        "authority_event_id": event_id,
        "lineage_count": len(results),
        "provider_quota_window": provider_observation,
        "effect_decision_counts": dict(sorted(decisions.items())),
        "external_effects_dispatched": external,
        "new_tasks_or_sessions_created": created,
        "results": results,
        "raw_session_ids_persisted": False,
        "safe_to_blind_retry": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES guarded initial logical lineage runtime")
    parser.add_argument("project", choices=sorted(SUPPORTED_PROJECTS))
    args = parser.parse_args(argv)
    result = run(args.project)
    print(json.dumps(result, sort_keys=True))
    if result.get("result") == _PROVIDER_READ_UNAVAILABLE_RESULT:
        return _PROVIDER_READ_UNAVAILABLE_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())