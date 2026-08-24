from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from .canary_orchestrator import WAITING_ANSWER_ACTION
from .idempotency import canonical_effect_identity, canonical_request_digest, effect_operation_key
from .identity import canonical_lane_id
from .live_runtime import build_live_state_store
from .operation_records import sanitize_receipt
from .providers.base import ProviderError, WriteOutcomeUnknown
from .providers.jules import JulesClient
from .state_store import (
    MutationAuthorization,
    StateUnavailable,
    WorkstreamRuntimeRecord,
    claim_operation,
    record_authoritative_readback,
    record_unknown_write,
)

SCHEMA_VERSION = "1.0"
OWNER_POLICY_EVENT_ID = "OWNER_2026_08_24_BOUNDED_EXISTING_SESSION_CONTINUATION"
OWNER_POLICY_EVIDENCE_ID = "UES_BOUNDED_EXISTING_SESSION_CONTINUATION_V1"
DEFAULT_TTL_SECONDS = 180


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _fingerprint(value: Any) -> str:
    return sha256(str(value or "").encode("utf-8")).hexdigest()


def _resource_name(value: Any) -> str:
    return str(value or "").strip().strip("/")


def _activity_id(activity: Mapping[str, Any]) -> str:
    return _resource_name(activity.get("name") or activity.get("id"))


def _activity_time(activity: Mapping[str, Any]) -> datetime | None:
    for key in ("createTime", "createdAt", "created_at", "timestamp", "updateTime"):
        parsed = _parse_time(activity.get(key))
        if parsed is not None:
            return parsed
    return None


def _message_event(activity: Mapping[str, Any], key: str) -> str | None:
    payload = activity.get(key)
    if not isinstance(payload, Mapping):
        return None
    field = "agentMessage" if key == "agentMessaged" else "userMessage"
    value = payload.get(field)
    return str(value) if isinstance(value, str) and value else None


def _latest_message(activities: list[dict[str, Any]], key: str) -> tuple[Mapping[str, Any], str, datetime] | None:
    candidates: list[tuple[Mapping[str, Any], str, datetime]] = []
    for activity in activities:
        message = _message_event(activity, key)
        when = _activity_time(activity)
        if message and when is not None:
            candidates.append((activity, message, when))
    return max(candidates, key=lambda item: item[2]) if candidates else None


def _load_adapter(project: str) -> dict[str, Any]:
    name = project.strip().lower()
    if name not in {"gs", "cep"}:
        raise ValueError("project must be GS or CEP")
    path = Path(__file__).resolve().parents[1] / "adapters" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _policy_entries(adapter: Mapping[str, Any]) -> list[dict[str, str]]:
    runtime = adapter.get("bounded_existing_session_runtime")
    if not isinstance(runtime, Mapping) or not runtime.get("enabled"):
        return []
    raw = runtime.get("waiting_continuations")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        workstream = str(item.get("workstream") or "").strip()
        starting_branch = str(item.get("starting_branch") or "").strip()
        response = str(item.get("response") or "").strip()
        if workstream and starting_branch and response:
            result.append({"workstream": workstream, "starting_branch": starting_branch, "response": response})
    return result


def _upsert_lane(*, project: str, route: str, workstream: str, session_fingerprint: str, source_fingerprint: str, source_repository: str, starting_branch: str):
    store = build_live_state_store()
    lane_id = canonical_lane_id(project, route, workstream)
    read = store.read_workstream(lane_id)
    if read.status == "MISSING":
        record = WorkstreamRuntimeRecord(lane_id=lane_id, project=project, route=route, workstream_id=workstream, activation_mode="ACTIVE_AUTO_SAFE")
        expected = 0
    elif read.status == "OK" and read.record is not None:
        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        expected = read.version
    else:
        raise StateUnavailable(read.reason or f"runtime lane unavailable: {lane_id}")
    record.activation_mode = "ACTIVE_AUTO_SAFE"
    record.actor_bindings = {
        "WRITER": {
            "provider": "jules",
            "session_fingerprint": session_fingerprint,
            "proof_status": "PROVEN_EXPLICIT_SOURCE",
            "source_repository": source_repository,
            "source_fingerprint": source_fingerprint,
            "starting_branch": starting_branch,
            "raw_session_id_persisted": False,
            "raw_source_id_persisted": False,
        }
    }
    record.authority_provenance = {
        "scope": "BOUNDED_EXISTING_SESSION_WAITING_CONTINUATION",
        "authority_event_id": OWNER_POLICY_EVENT_ID,
        "policy_evidence_id": OWNER_POLICY_EVIDENCE_ID,
        "new_task_creation_authorized": False,
        "merge_release_deploy_authorized": False,
    }
    saved = store.compare_and_swap_workstream(lane_id, expected, record)
    if saved.status != "OK" or saved.record is None:
        raise StateUnavailable(saved.reason or f"failed to save runtime lane: {lane_id}")
    return store, lane_id


def _safe_provider_evidence(receipt: Mapping[str, Any]) -> dict[str, Any]:
    activity = receipt.get("activity")
    return sanitize_receipt({
        "outcome": receipt.get("outcome"),
        "activity_fingerprint": _fingerprint(activity) if activity else None,
        "repository": receipt.get("repository"),
        "safe_to_blind_retry": False,
        "raw_session_id_persisted": False,
        "raw_source_id_persisted": False,
        "raw_activity_id_persisted": False,
    })


def _execute_one(*, client: JulesClient, adapter: Mapping[str, Any], policy: Mapping[str, str], session_name: str, source_name: str, activities: list[dict[str, Any]]) -> dict[str, Any]:
    project = str(adapter["project"])
    route = str(adapter["route"])
    repository = str(adapter["repository"])
    workstream = policy["workstream"]
    starting_branch = policy["starting_branch"]
    response = policy["response"]
    latest_agent = _latest_message(activities, "agentMessaged")
    latest_user = _latest_message(activities, "userMessaged")
    if latest_agent is None:
        return {"workstream": workstream, "decision": "NO_AGENT_WAITING_MESSAGE", "provider_write_attempted": False}
    agent_activity, agent_message, agent_time = latest_agent
    if latest_user is not None and latest_user[2] >= agent_time:
        return {
            "workstream": workstream,
            "decision": "WAITING_ALREADY_HAS_NEWER_OR_EQUAL_USER_RESPONSE",
            "provider_write_attempted": False,
            "latest_agent_message_fingerprint": _fingerprint(agent_message),
            "latest_user_message_fingerprint": _fingerprint(latest_user[1]),
        }
    waiting_activity_id = _activity_id(agent_activity)
    if not waiting_activity_id:
        return {"workstream": workstream, "decision": "WAITING_ACTIVITY_ID_MISSING", "provider_write_attempted": False}
    session_fp = _fingerprint(session_name)
    source_fp = _fingerprint(source_name)
    waiting_fp = _fingerprint(waiting_activity_id)
    store, lane_id = _upsert_lane(project=project, route=route, workstream=workstream, session_fingerprint=session_fp, source_fingerprint=source_fp, source_repository=repository, starting_branch=starting_branch)
    effect = canonical_effect_identity(
        lane_id=lane_id,
        project=project,
        route=route,
        workstream_id=workstream,
        action=WAITING_ANSWER_ACTION,
        target={"provider": "jules", "session_fingerprint": session_fp, "waiting_activity_fingerprint": waiting_fp},
    )
    operation_key = effect_operation_key(effect)
    request_digest = canonical_request_digest({"prompt": response})
    authorization = MutationAuthorization(
        effect_identity=effect,
        authority_event_id=OWNER_POLICY_EVENT_ID,
        project_policy_authorized=True,
        exact_binding_proven=True,
        evidence_verified=True,
        expires_at=_iso(_utc_now() + timedelta(minutes=10)),
    )
    claim = claim_operation(
        store,
        lane_id=lane_id,
        owner="ues-bounded-waiting-runtime",
        operation_key=operation_key,
        action=WAITING_ANSWER_ACTION,
        request_digest=request_digest,
        ttl_seconds=DEFAULT_TTL_SECONDS,
        receipt={"policy_evidence_id": OWNER_POLICY_EVIDENCE_ID, "session_fingerprint": session_fp, "waiting_activity_fingerprint": waiting_fp, "starting_branch": starting_branch},
        effect_identity=effect,
        authorization=authorization,
        observed_start={"provider_state": "AWAITING_USER_FEEDBACK", "session_fingerprint": session_fp, "waiting_activity_fingerprint": waiting_fp},
    )
    if claim.get("decision") != "CLAIMED" or not claim.get("mutation_allowed"):
        return {"workstream": workstream, "decision": str(claim.get("decision") or "CLAIM_DENIED"), "provider_write_attempted": False, "operation_key": operation_key}
    try:
        receipt = client.send_message(session_name, response, expected_repository=repository, expected_source=source_name)
    except WriteOutcomeUnknown as exc:
        evidence = {"category": "WRITE_OUTCOME_UNKNOWN", "safe_to_blind_retry": False}
        recovery = exc.to_dict().get("recovery") if hasattr(exc, "to_dict") else None
        if isinstance(recovery, Mapping):
            evidence["recovery_verdict"] = recovery.get("verdict")
            evidence["post_session_state"] = recovery.get("post_session_state")
        record_unknown_write(store, lane_id=lane_id, operation_key=operation_key, result=evidence)
        return {"workstream": workstream, "decision": "WRITE_OUTCOME_UNKNOWN_RECONCILIATION_REQUIRED", "provider_write_attempted": True, "operation_key": operation_key}
    except ProviderError as exc:
        record_unknown_write(store, lane_id=lane_id, operation_key=operation_key, result={"category": getattr(exc, "category", type(exc).__name__), "safe_to_blind_retry": False})
        return {"workstream": workstream, "decision": "PROVIDER_ERROR_RECONCILIATION_REQUIRED", "provider_write_attempted": True, "operation_key": operation_key}
    safe_evidence = _safe_provider_evidence(receipt)
    confirmed = record_authoritative_readback(store, lane_id=lane_id, operation_key=operation_key, observed=True, evidence=safe_evidence)
    return {
        "workstream": workstream,
        "decision": "BOUNDED_WAITING_CONTINUATION_CONFIRMED",
        "provider_write_attempted": True,
        "external_effects_dispatched": 1,
        "new_tasks_or_sessions_created": 0,
        "operation_key": operation_key,
        "operation_state": confirmed.record.state if confirmed.record else "CONFIRMED",
        "session_fingerprint": session_fp,
        "waiting_activity_fingerprint": waiting_fp,
        "latest_agent_message_fingerprint": _fingerprint(agent_message),
        "raw_session_id_persisted": False,
        "activity_content_persisted": False,
    }


def run(project: str) -> dict[str, Any]:
    adapter = _load_adapter(project)
    policies = _policy_entries(adapter)
    if not policies:
        return {"schema_version": SCHEMA_VERSION, "project": project.upper(), "result": "NO_BOUNDED_WAITING_POLICY", "provider_mutation_performed": False}
    key = str(os.environ.get("JULES_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("JULES_API_KEY is required")
    client = JulesClient(key)
    sessions = client.list_sessions(page_size=100)
    results: list[dict[str, Any]] = []
    matched: set[str] = set()
    for listed in sessions:
        session_name = _resource_name(listed.get("name"))
        if not session_name:
            continue
        full = client.get_session(session_name)
        state = str(full.get("normalizedState") or "UNKNOWN").upper()
        if state != "AWAITING_USER_FEEDBACK":
            continue
        source_name = _resource_name(full.get("sourceIdentifier"))
        if not source_name:
            continue
        source = client.get_source(source_name)
        repository = str(source.get("repository") or "")
        if repository.casefold() != str(adapter["repository"]).casefold():
            continue
        starting_branch = str(full.get("sourceStartingBranch") or "").strip()
        policy = next((item for item in policies if item["starting_branch"] == starting_branch), None)
        if policy is None:
            continue
        matched.add(policy["workstream"])
        activities = client.list_activities(session_name, page_size=100)
        results.append(_execute_one(client=client, adapter=adapter, policy=policy, session_name=session_name, source_name=source_name, activities=activities))
    for policy in policies:
        if policy["workstream"] not in matched:
            results.append({"workstream": policy["workstream"], "decision": "NO_MATCHING_AWAITING_SESSION", "provider_write_attempted": False})
    return {
        "schema_version": SCHEMA_VERSION,
        "project": str(adapter["project"]),
        "route": str(adapter["route"]),
        "result": "BOUNDED_WAITING_RUNTIME_COMPLETE",
        "authority_event_id": OWNER_POLICY_EVENT_ID,
        "policy_evidence_id": OWNER_POLICY_EVIDENCE_ID,
        "results": results,
        "external_effects_dispatched": sum(1 for item in results if item.get("decision") == "BOUNDED_WAITING_CONTINUATION_CONFIRMED"),
        "new_tasks_or_sessions_created": 0,
        "raw_session_ids_persisted": False,
        "activity_content_persisted": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES bounded existing-session waiting continuation runtime")
    parser.add_argument("project", choices=("GS", "CEP"))
    args = parser.parse_args(argv)
    result = run(args.project)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
