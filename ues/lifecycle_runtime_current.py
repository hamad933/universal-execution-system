from __future__ import annotations

import json
import os
from typing import Any, Mapping

from . import lifecycle_runtime as legacy
from . import lifecycle_runtime_v2 as runtime_v2
from .current_authority import load_current_authority_json
from .generation_reconciliation import reconcile_unknown_generation
from .jules_lifecycle import JulesLifecycleClient
from .lineage_observation import upsert_lineage_observation_preserving_effects
from .lineage_registry import lineage_lane_id
from .live_runtime import build_live_state_store
from .providers.github import GitHubClient
from .state_store import StateUnavailable
from .workflow_dispatch import dispatch_workflow_once, reconcile_unknown_workflow_dispatch

SCHEMA_VERSION = "2.2"


def _current_authority(adapter: Mapping[str, Any]) -> dict[str, Any] | None:
    return load_current_authority_json(
        adapter,
        os.environ.get("UES_CURRENT_AUTHORITY_JSON"),
        transport_actor=os.environ.get("UES_AUTHORITY_TRANSPORT_ACTOR") or os.environ.get("GITHUB_ACTOR"),
    )


def _legacy_recovery_with_current_authority(original: Any, authority: Mapping[str, Any] | None):
    """Gate every legacy provider-routing effect on a current authority envelope.

    Event/schedule/push wakeups are execution signals only. Without a validated
    current authority event the V2 runtime may observe and plan, but it must not
    send same-session Writer/Reviewer/waiting messages through the legacy effect
    executor. Generation creation and workflow dispatch have their own stricter
    current-authority gates.
    """

    event_id = str((authority or {}).get("authority_event_id") or "").strip()

    def execute(**kwargs: Any) -> dict[str, Any]:
        if not event_id:
            return {
                "decision": "CURRENT_AUTHORITY_REQUIRED_FOR_PROVIDER_EFFECT",
                "provider_write_attempted": False,
                "external_effects_dispatched": 0,
                "safe_to_blind_retry": False,
                "event_grants_mutation_authority": False,
            }
        return original(**kwargs)

    return execute


def _role_policies(config: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    result: list[tuple[str, Mapping[str, Any]]] = []
    for role, key in (
        ("WRITER", "writer"),
        ("REVIEWER", "reviewer"),
        ("ASSURANCE", "assurance"),
        ("FINAL_ASSURANCE", "final_assurance"),
    ):
        value = config.get(key)
        if isinstance(value, Mapping):
            result.append((role, value))
    return result


def _workstream_pr_number(config: Mapping[str, Any]) -> int | None:
    candidates: set[int] = set()
    direct = int(config.get("pr_number") or 0)
    if direct:
        candidates.add(direct)
    for _, policy in _role_policies(config):
        number = int(policy.get("pr_number") or 0)
        if number:
            candidates.add(number)
    if not candidates:
        return None
    if len(candidates) != 1:
        raise StateUnavailable("workstream current authority contains conflicting PR identities")
    return next(iter(candidates))


def _workstream_pr_state_current(
    github: GitHubClient,
    repository: str,
    config: Mapping[str, Any],
    ci_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    number = _workstream_pr_number(config)
    if number is None:
        return {"pr": None, "current_sha": None, "ci": None}
    owner, repo = legacy._repo_parts(repository)
    pr = github.get_pull_request(owner, repo, number)
    sha = str(pr.get("head_sha") or "") or None
    ci = github.get_required_ci_evidence(owner, repo, sha, ci_specs) if sha and ci_specs else None
    return {"pr": pr, "current_sha": sha, "ci": ci}


def _waiting_response(authority: Mapping[str, Any] | None, workstream: str, role: str) -> str | None:
    if not isinstance(authority, Mapping):
        return None
    waiting = authority.get("waiting_responses")
    waiting = waiting if isinstance(waiting, Mapping) else {}
    entry = waiting.get(f"{workstream}:{role.upper()}")
    if not isinstance(entry, Mapping):
        return None
    if entry.get("controller_resolvable") is not True or entry.get("scope_expansion") is True:
        return None
    response = str(entry.get("response") or "").strip()
    if not response:
        return None
    return response + "\n\n" + legacy.build_required_handoff_instructions(role, workstream)


def _runtime_with_authority_event(
    original: Any,
    authority: Mapping[str, Any] | None,
):
    def resolve(adapter: Mapping[str, Any]) -> dict[str, Any] | None:
        value = original(adapter)
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        if isinstance(authority, Mapping):
            event_id = str(authority.get("authority_event_id") or "").strip()
            if event_id:
                result["authority_event_id"] = event_id
        # Automatic generation remains owned only by runtime_v2 guarded path.
        result["auto_create_next_generation"] = False
        result["new_session_budget_safe"] = False
        return result
    return resolve


def _pre_reconcile_unknown_generations(
    *,
    adapter: Mapping[str, Any],
    authority: Mapping[str, Any] | None,
    store: Any,
    inventory: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    runtime = legacy._lineage_runtime(adapter) or {}
    workstreams = runtime_v2._merge_workstreams(runtime, authority)
    project = str(adapter.get("project") or "")
    route = str(adapter.get("route") or project)
    event_id = str((authority or {}).get("authority_event_id") or "").strip()
    results: list[dict[str, Any]] = []
    for workstream, config in workstreams.items():
        for role, _ in _role_policies(config):
            state_role = "ASSURANCE" if role == "FINAL_ASSURANCE" else role
            lane_id = lineage_lane_id(project, route, workstream, state_role)
            read = store.read_workstream(lane_id)
            if read.status != "OK" or read.record is None:
                continue
            pending = (read.record.evidence_bindings or {}).get("pending_generation_transition")
            if not read.record.unknown_write_state or not isinstance(pending, Mapping):
                continue
            if not event_id:
                results.append(
                    {
                        "workstream": workstream,
                        "role": role,
                        "decision": "GENERATION_UNKNOWN_CURRENT_AUTHORITY_REQUIRED_FOR_ADOPTION",
                        "safe_to_blind_retry": False,
                    }
                )
                continue
            result = reconcile_unknown_generation(
                store,
                project=project,
                route=route,
                workstream=workstream,
                role=role,
                inventory=inventory,
                authority_event_id=event_id,
                policy_provenance={"source": "DRIVE_CURRENT_STATE", "authority_event_id": event_id},
            )
            results.append({"workstream": workstream, "role": role, **result})
    return results


def _dispatch_requests(authority: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(authority, Mapping):
        return {}
    value = authority.get("workflow_dispatches")
    return value if isinstance(value, Mapping) else {}


def _handle_current_dispatches(
    *,
    adapter: Mapping[str, Any],
    authority: Mapping[str, Any] | None,
    store: Any,
    github: GitHubClient,
) -> list[dict[str, Any]]:
    requests = _dispatch_requests(authority)
    if not requests:
        return []
    event_id = str((authority or {}).get("authority_event_id") or "").strip()
    if not event_id:
        return [{"decision": "WORKFLOW_DISPATCH_CURRENT_AUTHORITY_REQUIRED", "provider_write_attempted": False}]

    policy_root = adapter.get("workflow_dispatch_policy")
    policy_root = policy_root if isinstance(policy_root, Mapping) else {}
    workflows = policy_root.get("workflows")
    workflows = workflows if isinstance(workflows, Mapping) else {}
    if policy_root.get("allow_arbitrary_workflow") is not False:
        raise StateUnavailable("bounded workflow dispatch policy is not fail-closed")

    runtime = legacy._lineage_runtime(adapter) or {}
    configs = runtime_v2._merge_workstreams(runtime, authority)
    ci_specs = legacy._required_ci_specs(adapter)
    repository = str(adapter.get("repository") or "")
    owner, repo = legacy._repo_parts(repository)
    project = str(adapter.get("project") or "")
    route = str(adapter.get("route") or project)
    results: list[dict[str, Any]] = []

    for workstream, raw in requests.items():
        if not isinstance(raw, Mapping) or raw.get("authorized") is not True:
            continue
        config = configs.get(str(workstream))
        if not isinstance(config, Mapping):
            results.append({"workstream": str(workstream), "decision": "WORKSTREAM_NOT_IN_CURRENT_AUTHORITY_OR_STABLE_TOPOLOGY"})
            continue
        key = str(raw.get("workflow_key") or "").strip()
        stable = workflows.get(key)
        if not isinstance(stable, Mapping):
            results.append({"workstream": str(workstream), "decision": "WORKFLOW_KEY_NOT_ALLOWLISTED"})
            continue
        workflow = str(stable.get("workflow") or "").strip()
        allowed_inputs = stable.get("allowed_inputs")
        allowed_inputs = allowed_inputs if isinstance(allowed_inputs, Mapping) else {}
        inputs = raw.get("inputs")
        inputs = {str(k): str(v) for k, v in inputs.items()} if isinstance(inputs, Mapping) else {}
        purpose = str(raw.get("purpose") or "").strip()
        if not workflow or not purpose:
            results.append({"workstream": str(workstream), "decision": "WORKFLOW_OR_PURPOSE_MISSING"})
            continue

        pr_state = _workstream_pr_state_current(github, repository, config, ci_specs)
        pr = pr_state.get("pr") if isinstance(pr_state.get("pr"), Mapping) else None
        sha = str(pr_state.get("current_sha") or "").strip()
        ref = str((pr or {}).get("head_ref") or "").strip()
        if not pr or not sha or not ref:
            results.append({"workstream": str(workstream), "decision": "EXACT_PR_HEAD_REQUIRED"})
            continue
        exact = github.verify_exact_head(owner, repo, ref, sha)
        if not exact.get("exact_head_match"):
            results.append({"workstream": str(workstream), "decision": "PR_HEAD_MOVED_BEFORE_DISPATCH"})
            continue

        effect_workstream = f"{workstream}-EVIDENCE::{key}"
        lane_id = legacy.canonical_lane_id(project, route, effect_workstream)
        lane = store.read_workstream(lane_id)
        try:
            if lane.status == "OK" and lane.record is not None and lane.record.unknown_write_state:
                result = reconcile_unknown_workflow_dispatch(
                    store,
                    github,
                    project=project,
                    route=route,
                    workstream=effect_workstream,
                    owner=owner,
                    repo=repo,
                    workflow=workflow,
                    ref=ref,
                    expected_sha=sha,
                    inputs=inputs,
                    purpose=purpose,
                )
            else:
                result = dispatch_workflow_once(
                    store,
                    github,
                    project=project,
                    route=route,
                    workstream=effect_workstream,
                    owner=owner,
                    repo=repo,
                    workflow=workflow,
                    ref=ref,
                    expected_sha=sha,
                    inputs=inputs,
                    allowed_workflows=[workflow],
                    allowed_inputs={str(k): [str(item) for item in v] for k, v in allowed_inputs.items() if isinstance(v, list)},
                    purpose=purpose,
                    authority_event_id=event_id,
                )
        except StateUnavailable as exc:
            result = {
                "decision": "WORKFLOW_DISPATCH_RECONCILIATION_REQUIRED",
                "reason": str(exc),
                "provider_write_attempted": False,
                "safe_to_blind_retry": False,
            }
        results.append({"workstream": str(workstream), "workflow_key": key, **result})
    return results


def run(project: str) -> dict[str, Any]:
    adapter = legacy._load_adapter(project)
    authority = _current_authority(adapter)
    key = str(os.environ.get("JULES_API_KEY") or "").strip()
    github_token = str(os.environ.get("GITHUB_TOKEN") or "").strip()
    if not key or not github_token:
        raise RuntimeError("JULES_API_KEY and GITHUB_TOKEN are required")

    store = build_live_state_store()
    jules = JulesLifecycleClient(key)
    github = GitHubClient(github_token)
    inventory = legacy._provider_inventory(jules)
    generation_reconciliation = _pre_reconcile_unknown_generations(
        adapter=adapter,
        authority=authority,
        store=store,
        inventory=inventory,
    )

    original_pr_state = legacy._workstream_pr_state
    original_waiting_prompt = legacy._waiting_prompt
    original_lineage_runtime = legacy._lineage_runtime
    original_execute_recovery = legacy._execute_recovery
    original_upsert = runtime_v2.upsert_lineage_observation
    try:
        legacy._workstream_pr_state = _workstream_pr_state_current
        legacy._waiting_prompt = lambda _adapter, workstream, role: _waiting_response(authority, workstream, role)
        legacy._lineage_runtime = _runtime_with_authority_event(original_lineage_runtime, authority)
        legacy._execute_recovery = _legacy_recovery_with_current_authority(original_execute_recovery, authority)
        runtime_v2.upsert_lineage_observation = upsert_lineage_observation_preserving_effects
        lifecycle = runtime_v2.run(project)
    finally:
        legacy._workstream_pr_state = original_pr_state
        legacy._waiting_prompt = original_waiting_prompt
        legacy._lineage_runtime = original_lineage_runtime
        legacy._execute_recovery = original_execute_recovery
        runtime_v2.upsert_lineage_observation = original_upsert

    dispatch_results = _handle_current_dispatches(
        adapter=adapter,
        authority=authority,
        store=store,
        github=github,
    )
    lifecycle = dict(lifecycle)
    lifecycle["schema_version"] = SCHEMA_VERSION
    lifecycle["generation_reconciliation"] = generation_reconciliation
    lifecycle["workflow_dispatch_results"] = dispatch_results
    lifecycle["current_authority_loaded"] = authority is not None
    lifecycle["current_authority_event_id"] = (authority or {}).get("authority_event_id")
    lifecycle["provider_routing_requires_current_authority"] = True
    lifecycle["unknown_effects_blind_retried"] = False
    return lifecycle


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="UES current-authority lifecycle integration")
    parser.add_argument("project", choices=["CEP", "GS", "cep", "gs"])
    args = parser.parse_args()
    print(json.dumps(run(args.project), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
