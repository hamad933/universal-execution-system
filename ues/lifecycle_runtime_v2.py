from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any, Mapping

from . import lifecycle_runtime as legacy
from .binding_safe_generation import execute_binding_safe_generation
from .current_authority import dynamic_lineages, exact_lineage_authority, load_current_authority_json
from .event_wakeup import register_wakeup
from .handoff_adjudication import exact_invalid_review_handoff_adjudication
from .jules_lifecycle import JulesLifecycleClient
from .lineage_generation import recover_lineage_policy_from_state
from .lineage_registry import DIRECT_CONTINUATION_STATES, lineage_lane_id, match_lineage_session, upsert_lineage_observation
from .live_runtime import build_live_state_store
from .policy_resolution import resolve_execution_policy
from .providers.github import GitHubClient
from .recovery_catalog import plan_recovery
from .state_store import StateUnavailable
from .structured_handoff import build_exact_review_handoff_instructions, build_required_handoff_instructions, find_latest_structured_handoff_runtime
from .task_budget import observe_rolling_quota_window

SCHEMA_VERSION = "2.0"
JULES_TASK_QUOTA_WINDOW_SECONDS = 24 * 60 * 60
_REF = re.compile(r"^[A-Za-z0-9._/-]+$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_STRUCTURED_HANDOFF_RECOVERY_CAUSE = "STRUCTURED_HANDOFF_RECOVERY_REQUIRED"


def _role_policy(config: Mapping[str, Any], role: str) -> Mapping[str, Any] | None:
    aliases = {
        "WRITER": "writer",
        "REVIEWER": "reviewer",
        "ASSURANCE": "assurance",
        "FINAL_ASSURANCE": "final_assurance",
    }
    value = config.get(aliases[role])
    return value if isinstance(value, Mapping) else None


def _configured_roles(config: Mapping[str, Any]) -> list[str]:
    roles: list[str] = []
    for role in ("WRITER", "REVIEWER", "ASSURANCE", "FINAL_ASSURANCE"):
        if _role_policy(config, role) is not None:
            roles.append(role)
    return roles


def _merge_workstreams(runtime: Mapping[str, Any], authority: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    stable = runtime.get("workstreams")
    if isinstance(stable, Mapping):
        for workstream, config in stable.items():
            if isinstance(config, Mapping):
                result[str(workstream)] = dict(config)
    for workstream, config in dynamic_lineages(authority).items():
        if not isinstance(config, Mapping):
            continue
        existing = dict(result.get(str(workstream), {}))
        for key, value in config.items():
            if isinstance(value, Mapping) and isinstance(existing.get(key), Mapping):
                existing[key] = {**dict(existing[key]), **dict(value)}
            else:
                existing[key] = value
        result[str(workstream)] = existing
    return result


def _state_snapshot(store: Any, *, project: str, route: str, workstream: str, role: str) -> dict[str, Any]:
    state_role = "ASSURANCE" if role == "FINAL_ASSURANCE" else role
    read = store.read_workstream(lineage_lane_id(project, route, workstream, state_role))
    if read.status != "OK" or read.record is None:
        return {}
    record = read.record
    evidence = record.evidence_bindings or {}
    return {
        "generation": int(evidence.get("generation") or 0),
        "session_fingerprint": evidence.get("session_fingerprint"),
        "unknown_write_state": record.unknown_write_state,
        "action_in_flight": record.action_in_flight,
        "operation_state": (record.operation_receipt or {}).get("state") if isinstance(record.operation_receipt, Mapping) else None,
    }


def _governed_for_lane(authority: Mapping[str, Any] | None, lane_authority: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(authority, Mapping):
        return {}
    value = dict(authority)
    generation = dict(value.get("generation_policy") or {}) if isinstance(value.get("generation_policy"), Mapping) else {}
    generation["necessary_generation_authorized"] = bool(lane_authority and lane_authority.get("authorized") is True)
    generation["generation_effect_authorized"] = bool(lane_authority and lane_authority.get("authorized") is True)
    value["generation_policy"] = generation
    return value


def _source_for_repository(client: JulesLifecycleClient, repository: str) -> tuple[str | None, bool]:
    matches: list[str] = []
    for source in client.list_sources(page_size=100):
        if legacy._source_repository(source) != repository:
            continue
        name = str(source.get("name") or "").strip().strip("/")
        if name:
            matches.append(name)
    unique = sorted(set(matches))
    return (unique[0], True) if len(unique) == 1 else (None, False)


def _active_duplicate_absent(
    inventory: list[dict[str, Any]],
    *,
    repository: str,
    starting_branch: str,
    current_session_fingerprint: str | None,
) -> bool:
    active = []
    for session in inventory:
        if str(session.get("_source_repository") or "").casefold() != repository.casefold():
            continue
        if str(session.get("sourceStartingBranch") or "") != starting_branch:
            continue
        state = str(session.get("normalizedState") or session.get("state") or "UNKNOWN").upper()
        if state in {"COMPLETED", "FAILED"}:
            continue
        fp = str(session.get("_session_fingerprint") or "")
        if current_session_fingerprint and fp == current_session_fingerprint:
            # An active current session is a reuse candidate, never evidence for replacement.
            active.append(session)
        else:
            active.append(session)
    return len(active) == 0


def _replacement_cause(lane_authority: Mapping[str, Any] | None) -> str | None:
    if not isinstance(lane_authority, Mapping):
        return None
    value = str(lane_authority.get("replacement_cause") or "").strip().upper()
    return value or None


def _policy_exact_baseline(policy: Mapping[str, Any]) -> tuple[str, str] | None:
    raw = str(policy.get("exact_baseline") or "").strip()
    if not raw:
        return None
    ref, sep, sha = raw.rpartition("@")
    ref = ref.strip()
    sha = sha.strip().lower()
    if ref.startswith("refs/heads/"):
        ref = ref[len("refs/heads/") :]
    elif ref.startswith("refs/"):
        raise StateUnavailable("lineage exact_baseline must reference a branch")
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
        raise StateUnavailable("lineage exact_baseline must be branch@40hex-sha")
    return ref, sha


def _candidate_sha(policy: Mapping[str, Any], pr_state: Mapping[str, Any]) -> str | None:
    explicit = _policy_exact_baseline(policy)
    if explicit is not None:
        return explicit[1]
    value = str(pr_state.get("current_sha") or "").strip().lower()
    return value or None


def _generation_preconditions(
    *,
    github: GitHubClient,
    repository: str,
    pr_state: Mapping[str, Any],
    policy: Mapping[str, Any],
    source_proven: bool,
) -> tuple[str | None, str | None, bool, bool]:
    owner, repo = legacy._repo_parts(repository)
    explicit = _policy_exact_baseline(policy)
    if explicit is not None:
        head_ref, candidate_sha = explicit
        pr = pr_state.get("pr") if isinstance(pr_state.get("pr"), Mapping) else None
        pr_sha = str(pr_state.get("current_sha") or "").strip().lower()
        pr_ref = str((pr or {}).get("head_ref") or "").strip()
        if pr_sha and pr_sha != candidate_sha:
            return head_ref, candidate_sha, source_proven, False
        if pr_ref and pr_ref != head_ref:
            return head_ref, candidate_sha, source_proven, False
        exact = github.verify_exact_head(owner, repo, head_ref, candidate_sha)
        return head_ref, candidate_sha, source_proven, bool(exact.get("exact_head_match"))

    pr = pr_state.get("pr") if isinstance(pr_state.get("pr"), Mapping) else None
    current_sha = str(pr_state.get("current_sha") or "").strip().lower()
    if pr is None or not current_sha:
        return None, None, source_proven, False
    head_ref = str(pr.get("head_ref") or "").strip()
    if not head_ref:
        return None, current_sha, source_proven, False
    exact = github.verify_exact_head(owner, repo, head_ref, current_sha)
    return head_ref, current_sha, source_proven, bool(exact.get("exact_head_match"))


def _replacement_prompt(role: str, workstream: str, policy: Mapping[str, Any], current_sha: str | None) -> str | None:
    template = str(policy.get("replacement_prompt") or "").strip()
    if not template:
        return None
    role_name = "ASSURANCE" if str(role).upper() == "FINAL_ASSURANCE" else str(role).upper()
    base = template.replace("{workstream}", workstream).replace("{current_sha}", current_sha or "UNKNOWN")
    if role_name in {"REVIEWER", "ASSURANCE"}:
        if not current_sha:
            return None
        return base + "\n\n" + build_exact_review_handoff_instructions(role_name, workstream, current_sha)
    return base + "\n\n" + build_required_handoff_instructions(role_name, workstream)


def _structured_handoff_recovery_ready(
    *,
    role: str,
    binding: Mapping[str, Any],
    handoff: Mapping[str, Any] | None,
    state_snapshot: Mapping[str, Any],
    handoff_invalidated: bool = False,
) -> bool:
    role_name = str(role).upper()
    provider_state = str(binding.get("provider_state") or "UNKNOWN").upper()
    return bool(
        role_name in {"REVIEWER", "ASSURANCE", "FINAL_ASSURANCE"}
        and binding.get("status") == "PROVEN"
        and provider_state == "COMPLETED"
        and (handoff is None or handoff_invalidated)
        and int(state_snapshot.get("generation") or 0) >= 1
        and str(state_snapshot.get("session_fingerprint") or "").strip()
        and not state_snapshot.get("unknown_write_state")
        and not state_snapshot.get("action_in_flight")
    )


def _load_wakeup_env() -> dict[str, Any] | None:
    event_id = str(os.environ.get("UES_WAKEUP_EVENT_ID") or "").strip()
    event_type = str(os.environ.get("UES_WAKEUP_EVENT_TYPE") or "").strip()
    if not event_id or not event_type:
        return None
    return {
        "event_id": event_id,
        "type": event_type,
        "source": str(os.environ.get("UES_WAKEUP_EVENT_SOURCE") or "github"),
        "repository": str(os.environ.get("UES_WAKEUP_REPOSITORY") or ""),
        "workstream": str(os.environ.get("UES_WAKEUP_WORKSTREAM") or ""),
        "sha": str(os.environ.get("UES_WAKEUP_SHA") or ""),
    }


def run(project: str) -> dict[str, Any]:
    adapter = legacy._load_adapter(project)
    runtime = legacy._lineage_runtime(adapter)
    if not runtime:
        return {"schema_version": SCHEMA_VERSION, "project": project.upper(), "result": "LINEAGE_RUNTIME_DISABLED"}

    repository = str(adapter.get("repository") or "")
    project_id = str(adapter.get("project") or project.upper())
    route = str(adapter.get("route") or project_id)
    authority = load_current_authority_json(
        adapter,
        os.environ.get("UES_CURRENT_AUTHORITY_JSON"),
        transport_actor=os.environ.get("UES_AUTHORITY_TRANSPORT_ACTOR") or os.environ.get("GITHUB_ACTOR"),
    )

    key = str(os.environ.get("JULES_API_KEY") or "").strip()
    github_token = str(os.environ.get("GITHUB_TOKEN") or "").strip()
    if not key or not github_token:
        raise RuntimeError("JULES_API_KEY and GITHUB_TOKEN are required")

    store = build_live_state_store()
    wakeup_event = _load_wakeup_env()
    wakeup = None
    if wakeup_event is not None:
        wakeup = register_wakeup(store, project=project_id, route=route, event=wakeup_event)
        if not wakeup.get("wakeup"):
            return {
                "schema_version": SCHEMA_VERSION,
                "project": project_id,
                "route": route,
                "result": "DUPLICATE_EVENT_COALESCED",
                "wakeup": wakeup,
            }

    legacy._persist_health(store, project=project_id, route=route, status="IN_FLIGHT", summary={"phase": "START", "runtime": "V2"})
    jules, github = JulesLifecycleClient(key), GitHubClient(github_token)
    inventory = legacy._provider_inventory(jules)
    ci_specs = legacy._required_ci_specs(adapter)
    workstream_configs = _merge_workstreams(runtime, authority)

    results: list[dict[str, Any]] = []
    bindings: dict[tuple[str, str], dict[str, Any]] = {}
    lineage_observations: dict[tuple[str, str], dict[str, Any]] = {}
    pr_states: dict[str, dict[str, Any]] = {}
    activities_cache: dict[str, list[dict[str, Any]]] = {}
    handoff_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    effective_policies: dict[tuple[str, str], dict[str, Any]] = {}

    for workstream, config in workstream_configs.items():
        # PR identity is technical routing supplied by current lineage authority or stable topology;
        # live branch/SHA are always re-read from GitHub.
        pr_states[workstream] = legacy._workstream_pr_state(github, repository, config, ci_specs)
        for role in _configured_roles(config):
            raw_policy = _role_policy(config, role)
            if raw_policy is None:
                continue
            state_role = "ASSURANCE" if role == "FINAL_ASSURANCE" else role
            policy = recover_lineage_policy_from_state(
                store,
                project=project_id,
                route=route,
                workstream=workstream,
                role=state_role,
                stable_policy=raw_policy,
            )
            binding = match_lineage_session(inventory, policy, repository=repository)
            bindings[(workstream, role)] = binding
            session = binding.get("session") if isinstance(binding.get("session"), Mapping) else None
            activities: list[dict[str, Any]] = []
            if session is not None:
                name = str(session.get("name") or "")
                if name:
                    if name not in activities_cache:
                        activities_cache[name] = jules.list_activities(name, page_size=100)
                    activities = activities_cache[name]
            handoff_cache[(workstream, role)] = (
                find_latest_structured_handoff_runtime(activities, expected_workstream=workstream, expected_role=state_role)
                if activities
                else None
            )
            lineage_observations[(workstream, role)] = upsert_lineage_observation(
                store,
                project=project_id,
                route=route,
                workstream=workstream,
                role=state_role,
                binding=binding,
                policy=policy,
                current_candidate_sha=_candidate_sha(raw_policy, pr_states[workstream]),
                current_pr_number=int(raw_policy.get("pr_number") or 0) or None,
            )

    # Jules currently meters tasks in a rolling 24-hour window. Keep the full
    # inventory for lineage/reconciliation, but feed only current-window usage to
    # the budget gate. No provider task-limit number is hard-coded here.
    provider_observation = {
        **observe_rolling_quota_window(
            inventory,
            window_seconds=JULES_TASK_QUOTA_WINDOW_SECONDS,
        ),
        "hard_provider_limit_reached": False,
    }

    for workstream, config in workstream_configs.items():
        pr_state = pr_states.get(workstream, {})
        ci = pr_state.get("ci") if isinstance(pr_state.get("ci"), Mapping) else {}
        pr = pr_state.get("pr") if isinstance(pr_state.get("pr"), Mapping) else {}
        work_remaining = bool(pr and not pr.get("merged") and str(pr.get("state") or "").lower() == "open")

        for role in _configured_roles(config):
            raw_policy = _role_policy(config, role)
            if raw_policy is None:
                continue
            binding = bindings.get((workstream, role), {"status": "UNBOUND"})
            session = binding.get("session") if isinstance(binding.get("session"), Mapping) else None
            activities = activities_cache.get(str(session.get("name") or ""), []) if session else []
            waiting = legacy._waiting_state(activities) if activities else {"newer_or_equal_user": None, "trigger_fingerprint": None}
            handoff_runtime = handoff_cache.get((workstream, role))
            handoff = legacy._safe_handoff(handoff_runtime)
            lane_authority = exact_lineage_authority(authority, workstream=workstream, role=role)
            governed = _governed_for_lane(authority, lane_authority)
            state_role = "ASSURANCE" if role == "FINAL_ASSURANCE" else role
            state_snapshot = _state_snapshot(store, project=project_id, route=route, workstream=workstream, role=role)
            resolved = resolve_execution_policy(
                adapter=adapter,
                governed_authority=governed,
                provider_observation=provider_observation,
                state_snapshot=state_snapshot,
            )
            effective = resolved.to_dict()
            effective_policies[(workstream, role)] = effective

            candidate_sha = _candidate_sha(raw_policy, pr_state)
            replacement_prompt = _replacement_prompt(state_role, workstream, raw_policy, candidate_sha)
            observation = {
                "binding_status": binding.get("status"),
                "provider_state": binding.get("provider_state"),
                "role": state_role,
                "handoff": handoff or {},
                "candidate_sha": candidate_sha if state_role == "WRITER" else None,
                "current_sha": candidate_sha,
                "ci_reason": ci.get("reason"),
                "ci_verdict": ci.get("verdict"),
                "pr_branch_match": legacy._pr_branch_match(pr_state, raw_policy),
                "waiting_has_newer_or_equal_user_response": waiting.get("newer_or_equal_user"),
                "same_session_prompt_ready": bool(legacy._waiting_prompt(adapter, workstream, state_role)),
                "work_remaining": work_remaining or role in {"ASSURANCE", "FINAL_ASSURANCE"},
                "new_session_budget_safe": bool(effective.get("generation_budget_safe")),
                "replacement_prompt_ready": bool(replacement_prompt),
                "replacement_required_proven": bool(lane_authority),
                "active_duplicate_absent": False,
                "unknown_write_state": bool(state_snapshot.get("unknown_write_state")),
            }

            recovery = dict(plan_recovery(observation))
            recovery["trigger_fingerprint"] = waiting.get("trigger_fingerprint")
            effect = None

            cause = _replacement_cause(lane_authority)
            generation_requested = bool(lane_authority and cause)
            if generation_requested:
                provider_state = str(binding.get("provider_state") or "UNKNOWN").upper()
                handoff_invalidated = exact_invalid_review_handoff_adjudication(
                    authority_event_id=str((authority or {}).get("authority_event_id") or ""),
                    lane_authority=lane_authority,
                    project=project_id,
                    route=route,
                    workstream=workstream,
                    role=role,
                    handoff=handoff,
                    binding=binding,
                    state_snapshot=state_snapshot,
                )
                if cause == _STRUCTURED_HANDOFF_RECOVERY_CAUSE and not _structured_handoff_recovery_ready(
                    role=role,
                    binding=binding,
                    handoff=handoff,
                    state_snapshot=state_snapshot,
                    handoff_invalidated=handoff_invalidated,
                ):
                    effect = {
                        "decision": "STRUCTURED_HANDOFF_RECOVERY_PRECONDITIONS_REQUIRED",
                        "provider_write_attempted": False,
                        "binding_status": binding.get("status"),
                        "provider_state": provider_state,
                        "structured_handoff_present": handoff is not None,
                        "structured_handoff_explicitly_invalidated": handoff_invalidated,
                        "safe_to_blind_retry": False,
                    }
                else:
                    source_name, source_proven = _source_for_repository(jules, repository)
                    start_ref, generation_sha, repo_proven, ref_proven = _generation_preconditions(
                        github=github,
                        repository=repository,
                        pr_state=pr_state,
                        policy=raw_policy,
                        source_proven=source_proven,
                    )
                    active_absent = bool(
                        start_ref
                        and _active_duplicate_absent(
                            inventory,
                            repository=repository,
                            starting_branch=start_ref,
                            current_session_fingerprint=str(binding.get("session_fingerprint") or "") or None,
                        )
                    )
                    observation["active_duplicate_absent"] = active_absent

                    # Reuse-first remains binding: an active exact-bound session must
                    # not be replaced merely because a generation is authorized.
                    active_exact_session = binding.get("status") == "PROVEN" and provider_state in DIRECT_CONTINUATION_STATES
                    if active_exact_session:
                        effect = {
                            "decision": "REUSE_ACTIVE_EXACT_SESSION",
                            "provider_write_attempted": False,
                            "safe_to_blind_retry": False,
                        }
                    elif source_name and start_ref and generation_sha and replacement_prompt:
                        effect = execute_binding_safe_generation(
                            store,
                            jules,
                            project=project_id,
                            route=route,
                            workstream=workstream,
                            role=role,
                            prompt=replacement_prompt,
                            title=f"{project_id} {workstream} {role}",
                            source_name=source_name,
                            starting_branch=start_ref,
                            repository=repository,
                            authority_event_id=str((authority or {}).get("authority_event_id") or ""),
                            current_policy=effective,
                            replacement_cause=cause,
                            candidate_sha=generation_sha,
                            work_remaining=observation["work_remaining"],
                            active_duplicate_absent=active_absent,
                            exact_repository_binding=repo_proven,
                            exact_starting_ref_binding=ref_proven,
                        )
                    else:
                        effect = {
                            "decision": "NEXT_GENERATION_EXACT_SOURCE_REF_OR_TASK_SPEC_REQUIRED",
                            "provider_write_attempted": False,
                            "safe_to_blind_retry": False,
                        }
            else:
                # Legacy same-session / completed-output routing is retained, but
                # automatic generation is forcibly disabled here; V2 owns it.
                runtime_no_generation = dict(runtime)
                runtime_no_generation["auto_create_next_generation"] = False
                paired_role = "REVIEWER" if role == "WRITER" else "WRITER"
                effect = legacy._execute_recovery(
                    adapter=adapter,
                    runtime=runtime_no_generation,
                    client=jules,
                    store=store,
                    repository=repository,
                    workstream=workstream,
                    role=state_role,
                    policy=raw_policy,
                    binding=binding,
                    recovery=recovery,
                    handoff_runtime=handoff_runtime,
                    pr_state=pr_state,
                    paired_binding=bindings.get((workstream, paired_role)),
                )

            lineage_state = lineage_observations.get((workstream, role), {})
            results.append(
                {
                    "workstream": workstream,
                    "role": role,
                    "binding_status": binding.get("status"),
                    "provider_state": binding.get("provider_state"),
                    "generation": lineage_state.get("generation"),
                    "current_sha": candidate_sha,
                    "ci_verdict": ci.get("verdict"),
                    "handoff": handoff,
                    "current_policy": effective,
                    "recovery": recovery,
                    "effect": effect,
                }
            )

    action_counts = Counter(str(item["recovery"].get("action") or "UNKNOWN") for item in results)
    effect_counts = Counter(str((item.get("effect") or {}).get("decision") or "NO_EFFECT") for item in results)
    summary = {
        "project": project_id,
        "runtime": "V2",
        "lineage_count": len(results),
        "provider_session_count": len(inventory),
        "provider_quota_window": provider_observation,
        "binding_counts": dict(sorted(Counter(str(item.get("binding_status") or "UNKNOWN") for item in results).items())),
        "recovery_action_counts": dict(sorted(action_counts.items())),
        "effect_decision_counts": dict(sorted(effect_counts.items())),
        "external_effects_dispatched": sum(int((item.get("effect") or {}).get("external_effects_dispatched") or 0) for item in results),
        "new_tasks_or_sessions_created": sum(int((item.get("effect") or {}).get("new_tasks_or_sessions_created") or 0) for item in results),
        "current_authority_loaded": authority is not None,
        "current_authority_event_id": (authority or {}).get("authority_event_id"),
        "event_wakeup": wakeup,
        "adapter_mutable_snapshot_is_authority": False,
        "state_store_generation_recovery_enabled": True,
        "same_session_reuse_first": True,
        "structured_handoff_recovery_is_same_logical_lineage": True,
    }
    health = legacy._persist_health(store, project=project_id, route=route, status="PASS", summary=summary)
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project_id,
        "route": route,
        "result": "LOGICAL_LINEAGE_LIFECYCLE_V2_COMPLETE",
        "summary": summary,
        "results": results,
        "health": health,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="UES current-authority logical lineage lifecycle V2")
    parser.add_argument("project", choices=["CEP", "GS", "cep", "gs"])
    args = parser.parse_args()
    print(json.dumps(run(args.project), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())