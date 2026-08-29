from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .binding_safe_generation import execute_binding_safe_generation
from .generation_transition import generation_transition_key
from .policy_resolution import resolve_execution_policy


def _workstream_contract(lane: Mapping[str, Any]) -> dict[str, Any]:
    task = lane["task_spec"]
    candidate = str(lane["candidate_sha"])
    return {
        "objective": str(task["objective"]),
        "exact_baseline": f"{lane['target_ref']}@{candidate}",
        "role": "ASSURANCE",
        "logical_lineage": str(lane["workstream"]),
        "write_scope": [],
        "prohibited_scope": list(task.get("prohibited_scope") or task.get("prohibitedScope") or []),
        "dependencies": [
            "current project authority still authorizes this evidence-supplement lineage",
            "independent evidence transport byte attestation is accepted by current project authority",
            "the predecessor physical generation is durably bound in StateStore",
        ],
        "validation": list(task.get("validation") or task.get("tests") or []),
        "evidence": list(task.get("evidence") or []),
        "handoff": str(task["handoff"]),
        "stop_gate": str(task.get("stop_gate") or task.get("stopGate")),
    }


def _continuation_prompt(runtime: Any, lane: Mapping[str, Any]) -> str:
    contract = _workstream_contract(lane)
    return (
        runtime._prompt(lane)
        + "\nPARENT_CONTROLLER_WORKSTREAM_CONTRACT_V1="
        + json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )


def _run_continuation_lane(
    runtime: Any,
    *,
    adapter: Mapping[str, Any],
    authority: Mapping[str, Any],
    store: Any,
    jules: Any,
    github: Any,
    inventory: Sequence[Mapping[str, Any]],
    provider_observation: Mapping[str, Any],
    actor: str,
    lane: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    project = str(adapter.get("project") or "").strip().upper()
    route = str(adapter.get("route") or project).strip()
    target_repository = str(adapter.get("repository") or "").strip()
    owner, repo = runtime.legacy._repo_parts(target_repository)

    exact = github.verify_exact_head(owner, repo, lane["target_ref"], lane["candidate_sha"])
    if not bool(exact.get("exact_head_match")):
        return {
            "workstream": lane["workstream"],
            "role": runtime._ALLOWED_ROLE,
            "candidate_sha": lane["candidate_sha"],
            "decision": "EVIDENCE_SUPPLEMENT_TARGET_CANDIDATE_MOVED",
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }

    try:
        source = runtime._resolve_unique_source(jules, lane["transport_repository_fingerprint"])
    except runtime._PROVIDER_READ_ERRORS as exc:
        return {
            "workstream": lane["workstream"],
            "role": runtime._ALLOWED_ROLE,
            "decision": "EVIDENCE_SUPPLEMENT_SOURCE_READ_UNAVAILABLE",
            "provider_read_error_category": getattr(exc, "category", type(exc).__name__),
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }
    except runtime.ProviderError as exc:
        return {
            "workstream": lane["workstream"],
            "role": runtime._ALLOWED_ROLE,
            "decision": "EVIDENCE_SUPPLEMENT_SOURCE_READ_FAILED",
            "provider_read_error_category": getattr(exc, "category", type(exc).__name__),
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }
    if source is None:
        return {
            "workstream": lane["workstream"],
            "role": runtime._ALLOWED_ROLE,
            "decision": "EVIDENCE_SUPPLEMENT_UNIQUE_PRIVATE_SOURCE_REQUIRED",
            "provider_write_attempted": False,
            "private_source_identity_persisted": False,
            "safe_to_blind_retry": False,
        }

    actual_source_name, actual_repository = source
    repository_alias = lane["transport_repository_fingerprint"]
    source_alias = "sha256:" + runtime.hashlib.sha256(actual_source_name.encode("utf-8")).hexdigest()
    sanitized = runtime._sanitized_inventory(
        inventory,
        actual_repository=actual_repository,
        repository_alias=repository_alias,
    )
    projected_authority = runtime._projected_authority(
        authority,
        key=f"{lane['workstream']}:{runtime._ALLOWED_ROLE}",
        task_spec=lane["task_spec"],
    )
    projected_adapter = dict(adapter)
    projected_adapter["repository"] = repository_alias
    effective = resolve_execution_policy(
        adapter=projected_adapter,
        governed_authority=projected_authority,
        provider_observation=provider_observation,
        state_snapshot=dict(state),
    ).to_dict()

    current_generation = int(state.get("generation") or 0)
    predecessor = str(state.get("session_fingerprint") or "").strip().lower() or None
    if current_generation < 1 or not predecessor:
        return {
            "workstream": lane["workstream"],
            "role": runtime._ALLOWED_ROLE,
            "decision": "EVIDENCE_SUPPLEMENT_PREDECESSOR_BINDING_REQUIRED",
            "provider_write_attempted": False,
            "safe_to_blind_retry": False,
        }

    replacement_cause = "TERMINAL_WITH_REMAINING_WORK"
    transition_key = generation_transition_key(
        project=project,
        route=route,
        workstream=lane["workstream"],
        role=runtime._ALLOWED_ROLE,
        current_generation=current_generation,
        predecessor_session_fingerprint=predecessor,
        candidate_sha=lane["candidate_sha"],
        replacement_cause=replacement_cause,
    )
    matches = runtime._marker_matches(
        sanitized,
        repository_alias=repository_alias,
        starting_branch=lane["transport_starting_branch"],
        marker=transition_key[:12],
    )
    if matches:
        return {
            "workstream": lane["workstream"],
            "role": runtime._ALLOWED_ROLE,
            "decision": "EVIDENCE_SUPPLEMENT_EXISTING_NEXT_GENERATION_MARKER_REQUIRES_ADJUDICATION",
            "provider_write_attempted": False,
            "match_count": len(matches),
            "safe_to_blind_retry": False,
        }

    client = runtime._SanitizedCreateClient(
        jules,
        actual_source_name=actual_source_name,
        actual_repository=actual_repository,
        source_alias=source_alias,
        repository_alias=repository_alias,
    )
    effect = execute_binding_safe_generation(
        store,
        client,
        project=project,
        route=route,
        workstream=lane["workstream"],
        role=runtime._ALLOWED_ROLE,
        prompt=_continuation_prompt(runtime, lane),
        title=f"{project} {lane['workstream']} ASSURANCE EVIDENCE SUPPLEMENT CONTINUATION",
        source_name=source_alias,
        starting_branch=lane["transport_starting_branch"],
        repository=repository_alias,
        authority_event_id=str(authority.get("authority_event_id") or ""),
        current_policy=effective,
        replacement_cause=replacement_cause,
        candidate_sha=lane["candidate_sha"],
        work_remaining=True,
        active_duplicate_absent=True,
        exact_repository_binding=True,
        exact_starting_ref_binding=True,
    )
    return {
        "workstream": lane["workstream"],
        "role": runtime._ALLOWED_ROLE,
        "candidate_sha": lane["candidate_sha"],
        "creation_kind": "EVIDENCE_SUPPLEMENT",
        "continuation_generation_from": current_generation,
        "transport_repository_fingerprint": repository_alias,
        "transport_starting_branch": lane["transport_starting_branch"],
        "transport_head_sha": lane["transport_head_sha"],
        "governed_packet_sha256": lane["governed_packet_sha256"],
        "decoded_evidence_sha256": lane["decoded_evidence_sha256"],
        "private_source_identity_persisted": False,
        "current_policy": effective,
        "effect": effect,
    }


def install() -> None:
    from . import evidence_supplement_runtime as runtime

    if getattr(runtime, "_same_lineage_continuation_installed", False):
        return
    original = runtime.run_evidence_supplements

    def patched_run_evidence_supplements(
        *,
        adapter: Mapping[str, Any],
        authority: Mapping[str, Any],
        entries: Mapping[str, Any],
        store: Any,
        jules: Any,
        github: Any,
        inventory: Sequence[Mapping[str, Any]],
        provider_observation: Mapping[str, Any],
        actor: str,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        initial_entries: dict[str, Any] = {}
        continuation: list[tuple[dict[str, Any], dict[str, Any]]] = []
        project = str(adapter.get("project") or "").strip().upper()
        route = str(adapter.get("route") or project).strip()

        for raw_key, raw_lane in entries.items():
            if not isinstance(raw_lane, Mapping) or raw_lane.get("authorized") is not True:
                initial_entries[str(raw_key)] = raw_lane
                continue
            try:
                lane = runtime._validate_lane(str(raw_key), raw_lane, now=current)
            except ValueError:
                initial_entries[str(raw_key)] = raw_lane
                continue
            state = runtime._state_snapshot(store, project=project, route=route, workstream=lane["workstream"])
            if int(state.get("generation") or 0) >= 1:
                continuation.append((lane, state))
            else:
                initial_entries[str(raw_key)] = raw_lane

        results = original(
            adapter=adapter,
            authority=authority,
            entries=initial_entries,
            store=store,
            jules=jules,
            github=github,
            inventory=inventory,
            provider_observation=provider_observation,
            actor=actor,
            now=current,
        )
        for lane, state in continuation:
            results.append(
                _run_continuation_lane(
                    runtime,
                    adapter=adapter,
                    authority=authority,
                    store=store,
                    jules=jules,
                    github=github,
                    inventory=inventory,
                    provider_observation=provider_observation,
                    actor=actor,
                    lane=lane,
                    state=state,
                )
            )
        return results

    runtime.run_evidence_supplements = patched_run_evidence_supplements
    runtime._same_lineage_continuation_installed = True
