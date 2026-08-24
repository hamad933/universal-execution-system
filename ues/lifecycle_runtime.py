from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from .jules_lifecycle import JulesLifecycleClient
from .lineage_effects import create_next_lineage_generation, send_same_lineage_message
from .lineage_registry import (
    DIRECT_CONTINUATION_STATES,
    match_lineage_session,
    session_fingerprint,
    upsert_lineage_observation,
)
from .live_runtime import build_live_state_store
from .providers.github import GitHubClient
from .recovery_catalog import plan_recovery
from .state_store import StateUnavailable, WorkstreamRuntimeRecord
from .structured_handoff import (
    build_required_handoff_instructions,
    find_latest_structured_handoff_runtime,
)
from .identity import canonical_lane_id

SCHEMA_VERSION = "1.0"
HEALTH_WORKSTREAM = "LIFECYCLE-RUNTIME-HEALTH"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_adapter(project: str) -> dict[str, Any]:
    name = str(project or "").strip().lower()
    if name not in {"gs", "cep"}:
        raise ValueError("project must be GS or CEP")
    path = Path(__file__).resolve().parents[1] / "adapters" / f"{name}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("project adapter must be an object")
    return value


def _lineage_runtime(adapter: Mapping[str, Any]) -> dict[str, Any]:
    value = adapter.get("lineage_runtime")
    if not isinstance(value, Mapping) or not value.get("enabled"):
        return {}
    workstreams = value.get("workstreams")
    if not isinstance(workstreams, Mapping):
        raise ValueError("lineage_runtime.workstreams must be an object")
    return dict(value)


def _required_ci_specs(adapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    profiles = adapter.get("evidence_profiles")
    if not isinstance(profiles, Mapping):
        return []
    specs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for profile in profiles.values():
        if not isinstance(profile, Mapping):
            continue
        requirements = profile.get("requirements")
        if not isinstance(requirements, list):
            continue
        for requirement in requirements:
            if not isinstance(requirement, Mapping):
                continue
            if str(requirement.get("provider") or "").upper() != "GITHUB_ACTIONS":
                continue
            if requirement.get("required") is False:
                continue
            workflow = requirement.get("workflow")
            job = str(requirement.get("job") or "").strip()
            if workflow in {None, ""}:
                continue
            spec = {"kind": "job", "workflow": workflow, "job": job} if job else {"kind": "workflow", "workflow": workflow}
            key = (spec["kind"], str(workflow), job)
            if key not in seen:
                specs.append(spec)
                seen.add(key)
    return specs


def _source_repository(source: Mapping[str, Any]) -> str | None:
    repository = source.get("repository")
    if isinstance(repository, str) and repository:
        return repository
    gh = source.get("githubRepo")
    if isinstance(gh, Mapping):
        owner, repo = gh.get("owner"), gh.get("repo")
        if isinstance(owner, str) and owner and isinstance(repo, str) and repo:
            return f"{owner}/{repo}"
    return None


def _provider_inventory(client: JulesLifecycleClient) -> list[dict[str, Any]]:
    sources = client.list_sources(page_size=100)
    source_by_name: dict[str, dict[str, Any]] = {}
    for source in sources:
        name = str(source.get("name") or "").strip().strip("/")
        if name:
            source_by_name[name] = source

    sessions: list[dict[str, Any]] = []
    for listed in client.list_sessions(page_size=100):
        name = str(listed.get("name") or "").strip().strip("/")
        if not name:
            continue
        full = client.get_session(name)
        source_name = str(full.get("sourceIdentifier") or "").strip().strip("/")
        source = source_by_name.get(source_name)
        if source is None and source_name:
            source = client.get_source(source_name)
            source_by_name[source_name] = source
        enriched = dict(full)
        enriched["_session_fingerprint"] = session_fingerprint(name)
        enriched["_source_name"] = source_name or None
        enriched["_source_repository"] = _source_repository(source or {})
        sessions.append(enriched)
    return sessions


def _activity_time(activity: Mapping[str, Any]) -> datetime | None:
    for key in ("createTime", "createdAt", "created_at", "timestamp", "updateTime"):
        text = str(activity.get(key) or "").strip()
        if not text:
            continue
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc)
    return None


def _latest_message(activities: list[dict[str, Any]], key: str, field: str) -> tuple[dict[str, Any], str, datetime] | None:
    candidates: list[tuple[dict[str, Any], str, datetime]] = []
    for activity in activities:
        payload = activity.get(key)
        if not isinstance(payload, Mapping):
            continue
        message = payload.get(field)
        when = _activity_time(activity)
        if isinstance(message, str) and message and when is not None:
            candidates.append((activity, message, when))
    return max(candidates, key=lambda item: item[2]) if candidates else None


def _waiting_state(activities: list[dict[str, Any]]) -> dict[str, Any]:
    agent = _latest_message(activities, "agentMessaged", "agentMessage")
    user = _latest_message(activities, "userMessaged", "userMessage")
    if agent is None:
        return {"latest_agent": None, "latest_user": user, "newer_or_equal_user": None, "trigger_fingerprint": None}
    newer = bool(user is not None and user[2] >= agent[2])
    activity_identity = str(agent[0].get("name") or agent[0].get("id") or agent[1])
    return {
        "latest_agent": agent,
        "latest_user": user,
        "newer_or_equal_user": newer,
        "trigger_fingerprint": sha256(activity_identity.encode("utf-8")).hexdigest(),
    }


def _repo_parts(repository: str) -> tuple[str, str]:
    pieces = repository.split("/", 1)
    if len(pieces) != 2 or not all(pieces):
        raise ValueError("adapter repository must be owner/repo")
    return pieces[0], pieces[1]


def _workstream_pr_state(
    github: GitHubClient,
    repository: str,
    config: Mapping[str, Any],
    ci_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    writer = config.get("writer") if isinstance(config.get("writer"), Mapping) else {}
    number = int(writer.get("pr_number") or 0)
    if not number:
        return {"pr": None, "current_sha": None, "ci": None}
    owner, repo = _repo_parts(repository)
    pr = github.get_pull_request(owner, repo, number)
    sha = str(pr.get("head_sha") or "") or None
    ci = github.get_required_ci_evidence(owner, repo, sha, ci_specs) if sha and ci_specs else None
    return {"pr": pr, "current_sha": sha, "ci": ci}


def _waiting_prompt(adapter: Mapping[str, Any], workstream: str, role: str) -> str | None:
    runtime = adapter.get("bounded_existing_session_runtime")
    if not isinstance(runtime, Mapping):
        return None
    entries = runtime.get("waiting_continuations")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, Mapping) or str(entry.get("workstream") or "") != workstream:
            continue
        prompt = str(entry.get("response") or "").strip()
        if prompt:
            return prompt + "\n\n" + build_required_handoff_instructions(role, workstream)
    return None


def _review_prompt(workstream: str, sha: str, pr_number: int | None) -> str:
    pr_text = f" PR #{pr_number}" if pr_number else ""
    return (
        f"Independently review {workstream}{pr_text} at exact candidate SHA {sha}. "
        "Do not modify code, branches, PRs, tests, or repository state. Inspect exact-SHA evidence and return PASS only if all applicable requirements are proven. "
        "If findings exist, provide actionable exact-SHA findings for the paired Writer lineage."
        "\n\n" + build_required_handoff_instructions("REVIEWER", workstream)
    )


def _correction_prompt(workstream: str, reviewed_sha: str | None, payload: Mapping[str, Any]) -> str:
    findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    serialized = json.dumps(findings, ensure_ascii=False, sort_keys=True)
    return (
        f"Continue the existing {workstream} Writer lineage and correct the independent review findings bound to exact SHA {reviewed_sha or 'UNKNOWN'}. "
        "Stay inside the already-governed workstream scope; do not weaken tests/governance or edit unrelated workstreams. "
        f"Findings: {serialized}"
        "\n\n" + build_required_handoff_instructions("WRITER", workstream)
    )


def _replacement_prompt(role: str, workstream: str, policy: Mapping[str, Any], current_sha: str | None) -> str | None:
    template = str(policy.get("replacement_prompt") or "").strip()
    if not template:
        return None
    return (
        template.replace("{workstream}", workstream).replace("{current_sha}", current_sha or "UNKNOWN")
        + "\n\n"
        + build_required_handoff_instructions(role, workstream)
    )


def _safe_handoff(handoff_runtime: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not handoff_runtime:
        return None
    sanitized = handoff_runtime.get("sanitized")
    return dict(sanitized) if isinstance(sanitized, Mapping) else None


def _persist_health(
    store: Any,
    *,
    project: str,
    route: str,
    status: str,
    summary: Mapping[str, Any],
    error_category: str | None = None,
) -> dict[str, Any]:
    lane_id = canonical_lane_id(project, route, HEALTH_WORKSTREAM)
    read = store.read_workstream(lane_id)
    if read.status == "MISSING":
        record = WorkstreamRuntimeRecord(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=HEALTH_WORKSTREAM,
            activation_mode="SHADOW",
        )
        expected = 0
    elif read.status == "OK" and read.record is not None:
        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        expected = read.version
    else:
        raise StateUnavailable(read.reason or "lifecycle health state unavailable")
    record.activation_mode = "SHADOW"
    record.actor_bindings = {}
    record.authority_provenance = {
        "scope": "LOGICAL_LINEAGE_LIFECYCLE_HEALTH",
        "telemetry_grants_no_authority": True,
        "raw_session_ids_persisted": False,
        "raw_activity_content_persisted": False,
    }
    record.last_observed_provider_state = {
        "status": status,
        "error_category": error_category,
        "summary": dict(summary),
        "observed_at": _iso_now(),
    }
    record.last_successful_transition = {
        "kind": "LOGICAL_LINEAGE_LIFECYCLE_CYCLE",
        "status": status,
        "at": _iso_now(),
    }
    saved = store.compare_and_swap_workstream(lane_id, expected, record)
    if saved.status != "OK":
        raise StateUnavailable(saved.reason or "failed to persist lifecycle health")
    return {"lane_id": lane_id, "version": saved.version, "status": status}


def _execute_recovery(
    *,
    adapter: Mapping[str, Any],
    runtime: Mapping[str, Any],
    client: JulesLifecycleClient,
    store: Any,
    repository: str,
    workstream: str,
    role: str,
    policy: Mapping[str, Any],
    binding: Mapping[str, Any],
    recovery: Mapping[str, Any],
    handoff_runtime: Mapping[str, Any] | None,
    pr_state: Mapping[str, Any],
    paired_binding: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not runtime.get("execute_safe_transitions", True):
        return None
    action = str(recovery.get("action") or "")
    session = binding.get("session") if isinstance(binding.get("session"), Mapping) else None
    if session is None:
        return None
    session_name = str(session.get("name") or "")
    source_name = str(session.get("_source_name") or "")
    if not session_name or not source_name:
        return None
    authority_event = str(runtime.get("authority_event_id") or "UES_LOGICAL_LINEAGE_LIFECYCLE_V1")

    if action == "CONTINUE_SAME_SESSION":
        prompt = _waiting_prompt(adapter, workstream, role)
        trigger = str(recovery.get("trigger_fingerprint") or "")
        if prompt and trigger:
            return send_same_lineage_message(
                store,
                client,
                project=str(adapter["project"]),
                route=str(adapter["route"]),
                workstream=workstream,
                role=role,
                session_name=session_name,
                source_name=source_name,
                repository=repository,
                prompt=prompt,
                trigger_fingerprint=trigger,
                authority_event_id=authority_event,
                action="waiting-answer",
            )

    if action == "ROUTE_STRUCTURED_FINDINGS_TO_WRITER_LINEAGE" and role in {"REVIEWER", "ASSURANCE"}:
        if not runtime.get("route_structured_findings", True) or not handoff_runtime or not paired_binding:
            return None
        writer_session = paired_binding.get("session") if isinstance(paired_binding.get("session"), Mapping) else None
        writer_state = str(paired_binding.get("provider_state") or "UNKNOWN").upper()
        if writer_session is None or writer_state not in DIRECT_CONTINUATION_STATES:
            return None
        payload = handoff_runtime.get("runtime_payload")
        sanitized = handoff_runtime.get("sanitized")
        if not isinstance(payload, Mapping) or not isinstance(sanitized, Mapping):
            return None
        writer_name = str(writer_session.get("name") or "")
        writer_source = str(writer_session.get("_source_name") or "")
        trigger = str(sanitized.get("activity_fingerprint") or sanitized.get("finding_payload_fingerprint") or "")
        if not writer_name or not writer_source or not trigger:
            return None
        prompt = _correction_prompt(workstream, sanitized.get("reviewed_sha"), payload)
        return send_same_lineage_message(
            store,
            client,
            project=str(adapter["project"]),
            route=str(adapter["route"]),
            workstream=workstream,
            role="WRITER",
            session_name=writer_name,
            source_name=writer_source,
            repository=repository,
            prompt=prompt,
            trigger_fingerprint=trigger,
            authority_event_id=authority_event,
            action="review-findings-correction",
        )

    if action == "ROUTE_CURRENT_SHA_TO_REVIEWER_LINEAGE" and role == "WRITER":
        if not runtime.get("route_writer_to_existing_reviewer", True) or not paired_binding:
            return None
        reviewer_session = paired_binding.get("session") if isinstance(paired_binding.get("session"), Mapping) else None
        reviewer_state = str(paired_binding.get("provider_state") or "UNKNOWN").upper()
        current_sha = str(pr_state.get("current_sha") or "")
        if reviewer_session is None or reviewer_state not in DIRECT_CONTINUATION_STATES or not current_sha:
            return None
        reviewer_name = str(reviewer_session.get("name") or "")
        reviewer_source = str(reviewer_session.get("_source_name") or "")
        if not reviewer_name or not reviewer_source:
            return None
        prompt = _review_prompt(workstream, current_sha, int(policy.get("pr_number") or 0) or None)
        return send_same_lineage_message(
            store,
            client,
            project=str(adapter["project"]),
            route=str(adapter["route"]),
            workstream=workstream,
            role="REVIEWER",
            session_name=reviewer_name,
            source_name=reviewer_source,
            repository=repository,
            prompt=prompt,
            trigger_fingerprint=current_sha,
            authority_event_id=authority_event,
            action="exact-sha-review-request",
        )

    if action == "CREATE_NEXT_SESSION_GENERATION_SAME_LINEAGE" and runtime.get("auto_create_next_generation"):
        generation = int(policy.get("next_generation") or 0)
        source_for_create = str(policy.get("source") or source_name)
        starting_branch = str(policy.get("starting_branch") or session.get("sourceStartingBranch") or "")
        prompt = _replacement_prompt(role, workstream, policy, str(pr_state.get("current_sha") or "") or None)
        if generation > 0 and source_for_create and starting_branch and prompt:
            return create_next_lineage_generation(
                store,
                client,
                project=str(adapter["project"]),
                route=str(adapter["route"]),
                workstream=workstream,
                role=role,
                predecessor_session_fingerprint=str(binding.get("session_fingerprint") or "") or None,
                next_generation=generation,
                prompt=prompt,
                title=f"{adapter['project']} {workstream} {role} G{generation}",
                source_name=source_for_create,
                starting_branch=starting_branch,
                repository=repository,
                authority_event_id=authority_event,
                budget_safe=bool(runtime.get("new_session_budget_safe")),
            )
    return None


def run(project: str) -> dict[str, Any]:
    adapter = _load_adapter(project)
    runtime = _lineage_runtime(adapter)
    if not runtime:
        return {"schema_version": SCHEMA_VERSION, "project": project.upper(), "result": "LINEAGE_RUNTIME_DISABLED"}

    repository = str(adapter.get("repository") or "")
    project_id = str(adapter.get("project") or project.upper())
    route = str(adapter.get("route") or project_id)
    key = str(os.environ.get("JULES_API_KEY") or "").strip()
    github_token = str(os.environ.get("GITHUB_TOKEN") or "").strip()
    if not key or not github_token:
        raise RuntimeError("JULES_API_KEY and GITHUB_TOKEN are required")

    store = build_live_state_store()
    _persist_health(store, project=project_id, route=route, status="IN_FLIGHT", summary={"phase": "START"})
    jules = JulesLifecycleClient(key)
    github = GitHubClient(github_token)
    inventory = _provider_inventory(jules)
    ci_specs = _required_ci_specs(adapter)
    workstream_configs = runtime["workstreams"]

    results: list[dict[str, Any]] = []
    bindings: dict[tuple[str, str], dict[str, Any]] = {}
    pr_states: dict[str, dict[str, Any]] = {}
    activities_cache: dict[str, list[dict[str, Any]]] = {}
    handoff_cache: dict[tuple[str, str], dict[str, Any] | None] = {}

    for workstream, raw_config in workstream_configs.items():
        if not isinstance(raw_config, Mapping):
            continue
        config = dict(raw_config)
        pr_states[str(workstream)] = _workstream_pr_state(github, repository, config, ci_specs)
        for role in ("WRITER", "REVIEWER"):
            policy = config.get(role.lower())
            if not isinstance(policy, Mapping):
                continue
            binding = match_lineage_session(inventory, policy, repository=repository)
            bindings[(str(workstream), role)] = binding
            session = binding.get("session") if isinstance(binding.get("session"), Mapping) else None
            activities: list[dict[str, Any]] = []
            if session is not None:
                session_name = str(session.get("name") or "")
                if session_name:
                    if session_name not in activities_cache:
                        activities_cache[session_name] = jules.list_activities(session_name, page_size=100)
                    activities = activities_cache[session_name]
            handoff_cache[(str(workstream), role)] = find_latest_structured_handoff_runtime(
                activities,
                expected_workstream=str(workstream),
                expected_role=role,
            ) if activities else None
            upsert_lineage_observation(
                store,
                project=project_id,
                route=route,
                workstream=str(workstream),
                role=role,
                binding=binding,
                policy=policy,
                current_candidate_sha=pr_states[str(workstream)].get("current_sha"),
                current_pr_number=int(policy.get("pr_number") or 0) or None,
            )

    for workstream, raw_config in workstream_configs.items():
        if not isinstance(raw_config, Mapping):
            continue
        config = dict(raw_config)
        pr_state = pr_states.get(str(workstream), {})
        ci = pr_state.get("ci") if isinstance(pr_state.get("ci"), Mapping) else {}
        pr = pr_state.get("pr") if isinstance(pr_state.get("pr"), Mapping) else {}
        work_remaining = bool(pr and not pr.get("merged") and str(pr.get("state") or "").lower() == "open")

        for role in ("WRITER", "REVIEWER"):
            policy = config.get(role.lower())
            if not isinstance(policy, Mapping):
                continue
            binding = bindings.get((str(workstream), role), {"status": "UNBOUND"})
            session = binding.get("session") if isinstance(binding.get("session"), Mapping) else None
            activities = activities_cache.get(str(session.get("name") or ""), []) if session else []
            waiting = _waiting_state(activities) if activities else {"newer_or_equal_user": None, "trigger_fingerprint": None}
            handoff_runtime = handoff_cache.get((str(workstream), role))
            handoff = _safe_handoff(handoff_runtime)
            replacement_prompt = _replacement_prompt(role, str(workstream), policy, pr_state.get("current_sha"))
            observation = {
                "binding_status": binding.get("status"),
                "provider_state": binding.get("provider_state"),
                "role": role,
                "handoff": handoff or {},
                "candidate_sha": pr_state.get("current_sha") if role == "WRITER" else None,
                "current_sha": pr_state.get("current_sha"),
                "ci_reason": ci.get("reason"),
                "ci_verdict": ci.get("verdict"),
                "waiting_has_newer_or_equal_user_response": waiting.get("newer_or_equal_user"),
                "same_session_prompt_ready": bool(_waiting_prompt(adapter, str(workstream), role)),
                "work_remaining": work_remaining,
                "new_session_budget_safe": bool(runtime.get("new_session_budget_safe")),
                "replacement_prompt_ready": bool(replacement_prompt),
                "unknown_write_state": False,
            }
            recovery = plan_recovery(observation)
            recovery = dict(recovery)
            recovery["trigger_fingerprint"] = waiting.get("trigger_fingerprint")
            paired_role = "REVIEWER" if role == "WRITER" else "WRITER"
            effect = _execute_recovery(
                adapter=adapter,
                runtime=runtime,
                client=jules,
                store=store,
                repository=repository,
                workstream=str(workstream),
                role=role,
                policy=policy,
                binding=binding,
                recovery=recovery,
                handoff_runtime=handoff_runtime,
                pr_state=pr_state,
                paired_binding=bindings.get((str(workstream), paired_role)),
            )
            results.append({
                "workstream": str(workstream),
                "role": role,
                "binding_status": binding.get("status"),
                "provider_state": binding.get("provider_state"),
                "generation": None,
                "current_sha": pr_state.get("current_sha"),
                "ci_verdict": ci.get("verdict"),
                "handoff": handoff,
                "recovery": recovery,
                "effect": effect,
            })

    action_counts = Counter(str(item["recovery"].get("action") or "UNKNOWN") for item in results)
    effect_counts = Counter(
        str((item.get("effect") or {}).get("decision") or "NO_EFFECT") for item in results
    )
    summary = {
        "project": project_id,
        "lineage_count": len(results),
        "provider_session_count": len(inventory),
        "binding_counts": dict(sorted(Counter(str(item.get("binding_status") or "UNKNOWN") for item in results).items())),
        "provider_state_counts": dict(sorted(Counter(str(item.get("provider_state") or "UNKNOWN") for item in results).items())),
        "recovery_action_counts": dict(sorted(action_counts.items())),
        "effect_decision_counts": dict(sorted(effect_counts.items())),
        "external_effects_dispatched": sum(int((item.get("effect") or {}).get("external_effects_dispatched") or 0) for item in results),
        "new_tasks_or_sessions_created": sum(int((item.get("effect") or {}).get("new_tasks_or_sessions_created") or 0) for item in results),
        "raw_session_ids_persisted": False,
        "raw_activity_content_persisted": False,
        "structured_handoff_required_for_new_generations": True,
    }
    health = _persist_health(store, project=project_id, route=route, status="PASS", summary=summary)
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project_id,
        "route": route,
        "result": "LOGICAL_LINEAGE_LIFECYCLE_COMPLETE",
        "summary": summary,
        "results": results,
        "health": health,
    }


def run_supervised(project: str) -> dict[str, Any]:
    try:
        return run(project)
    except Exception as exc:
        try:
            adapter = _load_adapter(project)
            store = build_live_state_store()
            health = _persist_health(
                store,
                project=str(adapter.get("project") or project.upper()),
                route=str(adapter.get("route") or project.upper()),
                status="FAIL",
                summary={"phase": "FAILED"},
                error_category=str(getattr(exc, "category", None) or type(exc).__name__).upper()[:120],
            )
        except Exception:
            health = {"status": "FAIL", "health_persistence": "FAILED"}
        return {
            "schema_version": SCHEMA_VERSION,
            "project": project.upper(),
            "result": "LOGICAL_LINEAGE_LIFECYCLE_FAILED",
            "error_category": str(getattr(exc, "category", None) or type(exc).__name__).upper()[:120],
            "exception_text_persisted": False,
            "health": health,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES logical Writer/Reviewer lineage lifecycle runtime")
    parser.add_argument("project", choices=("GS", "CEP"))
    args = parser.parse_args(argv)
    result = run_supervised(args.project)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("result") == "LOGICAL_LINEAGE_LIFECYCLE_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
