from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Mapping, Sequence

from .lineage_generation import _confirmed_receipt_binding
from .operation_records import sanitize_receipt
from .structured_handoff import START_MARKER, find_latest_structured_handoff_runtime

SCHEMA_VERSION = "1.0"
TERMINAL_STATE = "COMPLETED"
TERMINAL_ATTENTION_CLASSIFICATION = "COMPLETED_OUTPUT_REQUIRES_CONSUMPTION_CHECK"


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(raw.encode("utf-8")).hexdigest()


def _bounded(value: Any, limit: int = 1000) -> str | None:
    if value is None:
        return None
    text = str(sanitize_receipt(value)).strip()
    return text[:limit] if text else None


def _agent_messages(activities: Sequence[Mapping[str, Any]]) -> list[str]:
    messages: list[str] = []
    for activity in activities:
        payload = activity.get("agentMessaged")
        if not isinstance(payload, Mapping):
            continue
        message = payload.get("agentMessage")
        if isinstance(message, str) and message:
            messages.append(message)
    return messages


def extract_terminal_candidate(activities: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Extract only sanitized structured content from runtime-only provider Activities."""
    runtime = find_latest_structured_handoff_runtime(activities)
    if runtime is None:
        marker_seen = any(START_MARKER in message for message in _agent_messages(activities))
        return {
            "schema_version": SCHEMA_VERSION,
            "state": "MALFORMED_STRUCTURED_HANDOFF" if marker_seen else "COMPLETED_OUTPUT_UNSTRUCTURED",
            "structured": False,
            "raw_activity_content_persisted": False,
            "raw_session_id_persisted": False,
        }
    sanitized = runtime.get("sanitized") if isinstance(runtime, Mapping) else None
    payload = runtime.get("runtime_payload") if isinstance(runtime, Mapping) else None
    if not isinstance(sanitized, Mapping) or not isinstance(payload, Mapping):
        return {
            "schema_version": SCHEMA_VERSION,
            "state": "MALFORMED_STRUCTURED_HANDOFF",
            "structured": False,
            "raw_activity_content_persisted": False,
            "raw_session_id_persisted": False,
        }
    raw_findings = payload.get("findings")
    raw_findings = raw_findings if isinstance(raw_findings, list) else []
    findings: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_findings):
        if not isinstance(raw, Mapping):
            continue
        evidence = raw.get("evidence_references") or raw.get("evidence") or []
        if isinstance(evidence, (str, bytes)):
            evidence = [evidence]
        refs = [_bounded(item, 500) for item in evidence[:20]] if isinstance(evidence, list) else []
        findings.append({
            "finding_id": _bounded(raw.get("id") or raw.get("finding_id"), 160) or f"finding-{index + 1}",
            "severity": (_bounded(raw.get("severity"), 80) or "UNKNOWN").upper(),
            "path": _bounded(raw.get("path"), 500),
            "resource": _bounded(raw.get("resource"), 500),
            "locator": _bounded(raw.get("locator") or raw.get("line") or raw.get("selector"), 500),
            "summary": _bounded(raw.get("summary") or raw.get("detail"), 1200),
            "recommended_action": _bounded(raw.get("recommended_action") or raw.get("recommended_remediation") or raw.get("action"), 1200),
            "evidence_references": [item for item in refs if item],
        })
    candidate = {
        "schema_version": SCHEMA_VERSION,
        "state": "STRUCTURED_HANDOFF_EXTRACTED",
        "structured": True,
        "role": str(sanitized.get("role") or "").upper(),
        "workstream": str(sanitized.get("workstream") or ""),
        "status": str(sanitized.get("status") or "").upper(),
        "verdict": str(sanitized.get("verdict") or "UNKNOWN").upper(),
        "candidate_sha": sanitized.get("candidate_sha"),
        "reviewed_sha": sanitized.get("reviewed_sha"),
        "context_state": str(sanitized.get("context_state") or "UNKNOWN").upper(),
        "finding_count": len(findings),
        "findings": findings,
        "activity_fingerprint": sanitized.get("activity_fingerprint"),
        "message_fingerprint": sanitized.get("message_fingerprint"),
        "raw_activity_content_persisted": False,
        "raw_session_id_persisted": False,
    }
    candidate["handoff_fingerprint"] = _fingerprint(candidate)
    return candidate


def _lineage_identity_from_record(record: Any) -> tuple[str, str]:
    evidence = record.evidence_bindings or {}
    role = str(evidence.get("role") or "").strip().upper()
    workstream = str(evidence.get("workstream") or "").strip()
    if role and workstream:
        return role, workstream
    runtime_id = str(getattr(record, "workstream_id", "") or "").strip()
    if runtime_id.startswith("LINEAGE::") and "::" in runtime_id[len("LINEAGE::"):]:
        body = runtime_id[len("LINEAGE::"):]
        inferred_workstream, inferred_role = body.rsplit("::", 1)
        role = role or inferred_role.strip().upper()
        workstream = workstream or inferred_workstream.strip()
    return role, workstream


def _valid_session_fingerprint(value: Any) -> str | None:
    fp = str(value or "").strip().lower()
    if len(fp) != 64 or any(ch not in "0123456789abcdef" for ch in fp):
        return None
    return fp


def _confirmed_generation_operation_bindings(
    store: Any,
    *,
    project: str,
    route: str,
) -> dict[str, list[tuple[str, int, str, str, str]]]:
    """Recover exact historical generations from confirmed provider-write receipts only."""

    discover = getattr(store, "discover_operation_keys", None)
    if not callable(discover):
        return {}

    bindings: dict[str, list[tuple[str, int, str, str, str]]] = {}
    supported_actions = {"create-session-generation", "create-initial-lineage-session"}
    for operation_key in discover():
        read = store.read_operation(operation_key)
        if read.status != "OK" or read.record is None:
            continue
        record = read.record
        action = str(getattr(record, "action", "") or "")
        if action not in supported_actions:
            continue
        if str(getattr(record, "state", "") or "").upper() != "CONFIRMED":
            continue

        effect = getattr(record, "effect_identity", None)
        if not isinstance(effect, Mapping):
            continue
        lane_id = str(getattr(record, "lane_id", "") or "").strip()
        workstream_id = str(getattr(record, "workstream_id", "") or "").strip()
        if (
            str(effect.get("project") or "") != project
            or str(effect.get("route") or "") != route
            or str(effect.get("lane_id") or "") != lane_id
            or str(effect.get("workstream_id") or "") != workstream_id
            or str(effect.get("action") or "") != action
        ):
            continue
        if not workstream_id.startswith("LINEAGE::") or "::" not in workstream_id[len("LINEAGE::"):]:
            continue
        lineage_body = workstream_id[len("LINEAGE::"):]
        workstream, role = lineage_body.rsplit("::", 1)
        workstream = workstream.strip()
        role = role.strip().upper()
        if not workstream or not role:
            continue

        target = effect.get("target")
        if not isinstance(target, Mapping) or str(target.get("role") or "").upper() != role:
            continue
        try:
            generation = int(target.get("generation") or 0)
        except (TypeError, ValueError):
            continue
        if generation <= 0:
            continue
        if action == "create-initial-lineage-session" and (
            generation != 1
            or str(target.get("creation_kind") or "") != "INITIAL_LOGICAL_LINEAGE"
        ):
            continue

        readback = getattr(record, "authoritative_readback", None)
        if not isinstance(readback, Mapping) or readback.get("observed") is not True:
            continue
        evidence = readback.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        fp = _valid_session_fingerprint(evidence.get("session_fingerprint"))
        try:
            observed_generation = int(evidence.get("generation") or 0)
        except (TypeError, ValueError):
            continue
        if fp is None or observed_generation != generation:
            continue
        if action == "create-initial-lineage-session" and str(evidence.get("creation_kind") or "") != "INITIAL_LOGICAL_LINEAGE":
            continue

        recovery_source = (
            "CONFIRMED_INITIAL_LINEAGE_OPERATION"
            if action == "create-initial-lineage-session"
            else "CONFIRMED_GENERATION_OPERATION"
        )
        bindings.setdefault(lane_id, []).append(
            (fp, generation, recovery_source, workstream, role)
        )
    return bindings


def lineage_index(store: Any, *, project: str, route: str) -> dict[str, list[dict[str, Any]]]:
    """Index every exact durable lineage generation still proven by StateStore.

    Confirmed operation readbacks are authoritative historical-generation proof.
    Current evidence and confirmed lane receipts remain exact proof sources. The
    convenience previous-session field is used only when no confirmed operation
    proof exists for that generation. Conflicting confirmed proofs still fail
    closed; no title, time, ordering, or repository inference is permitted.
    """
    discover = getattr(store, "discover_lane_ids", None)
    if not callable(discover):
        return {}
    operation_bindings = _confirmed_generation_operation_bindings(
        store,
        project=project,
        route=route,
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for lane_id in discover():
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            continue
        record = read.record
        if record.project != project or record.route != route:
            continue
        evidence = record.evidence_bindings or {}
        generation = int(evidence.get("generation") or 0)
        role, workstream = _lineage_identity_from_record(record)
        if not role or not workstream:
            continue

        lane_operations = [
            item
            for item in operation_bindings.get(lane_id, [])
            if item[3] == workstream and item[4] == role
        ]
        operation_generations = {item[1] for item in lane_operations}

        bindings: list[tuple[str, int, str]] = []
        current_fp = str(evidence.get("session_fingerprint") or "").strip().lower()
        if current_fp and generation > 0:
            bindings.append((current_fp, generation, "EVIDENCE_BINDINGS"))

        previous_fp = str(evidence.get("previous_session_fingerprint") or "").strip().lower()
        previous_generation = generation - 1
        if (
            previous_fp
            and previous_generation > 0
            and previous_generation not in operation_generations
            and len(previous_fp) == 64
            and all(ch in "0123456789abcdef" for ch in previous_fp)
        ):
            bindings.append((previous_fp, previous_generation, "PREVIOUS_SESSION_FINGERPRINT"))

        receipt_binding = _confirmed_receipt_binding(record)
        if receipt_binding:
            receipt_fp = str(receipt_binding.get("session_fingerprint") or "").strip().lower()
            receipt_generation = int(receipt_binding.get("generation") or 0)
            if receipt_fp and receipt_generation > 0:
                bindings.append((receipt_fp, receipt_generation, "CONFIRMED_CREATION_RECEIPT"))

        for fp, binding_generation, recovery_source, _, _ in lane_operations:
            bindings.append((fp, binding_generation, recovery_source))

        fingerprints_by_generation: dict[int, set[str]] = {}
        for fp, binding_generation, _ in bindings:
            fingerprints_by_generation.setdefault(binding_generation, set()).add(fp)
        conflicted_generations = {
            binding_generation
            for binding_generation, fingerprints in fingerprints_by_generation.items()
            if len(fingerprints) > 1
        }

        seen: set[tuple[str, int, str, str]] = set()
        for fp, binding_generation, recovery_source in bindings:
            if binding_generation in conflicted_generations:
                continue
            key = (fp, binding_generation, role, workstream)
            if key in seen:
                continue
            seen.add(key)
            result.setdefault(fp, []).append({
                "lane_id": lane_id,
                "role": role,
                "workstream": workstream,
                "generation": binding_generation,
                "current_candidate_sha": evidence.get("current_candidate_sha"),
                "current_pr_number": evidence.get("current_pr_number"),
                "identity_recovery_source": recovery_source,
            })
    return result


def _identity_result(*, project: str, route: str, repository: str, fp: str, reason: str) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "route": route,
        "logical_workstream": None,
        "role": None,
        "generation": None,
        "session_fingerprint": fp or None,
        "repository": repository,
        "status": "COMPLETE",
        "verdict": None,
        "finding_count": None,
        "findings": [],
        "result_state": "RESULT_IDENTITY_UNRESOLVED",
        "identity_reason": reason,
        "freshness_status": "UNBOUND",
        "parent_action_required": True,
        "raw_activity_content_persisted": False,
        "raw_session_id_persisted": False,
    }
    result["result_fingerprint"] = _fingerprint(result)
    return result


def _bound_result(*, project: str, route: str, repository: str, session: Mapping[str, Any], candidate: Mapping[str, Any], lineage: Mapping[str, Any]) -> dict[str, Any]:
    fp = str(session.get("session_fingerprint") or "")
    role = str(lineage.get("role") or "").upper()
    workstream = str(lineage.get("workstream") or "")
    current_sha = str(lineage.get("current_candidate_sha") or "") or None
    claimed_role = str(candidate.get("role") or "").upper()
    claimed_workstream = str(candidate.get("workstream") or "")
    base = {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "route": route,
        "logical_workstream": workstream,
        "role": role,
        "generation": int(lineage.get("generation") or 0),
        "session_fingerprint": fp,
        "repository": repository,
        "status": candidate.get("status"),
        "verdict": candidate.get("verdict"),
        "reviewed_sha": candidate.get("reviewed_sha"),
        "candidate_sha": candidate.get("candidate_sha"),
        "finding_count": int(candidate.get("finding_count") or 0),
        "findings": list(candidate.get("findings") or []),
        "context_state": candidate.get("context_state"),
        "handoff_fingerprint": candidate.get("handoff_fingerprint"),
        "freshness_status": "FRESH",
        "result_state": "PARENT_CONSUMABLE",
        "raw_activity_content_persisted": False,
        "raw_session_id_persisted": False,
    }
    if str(session.get("source_repository") or "").casefold() != repository.casefold() or session.get("source_binding_proven") is not True:
        base["result_state"] = "RESULT_IDENTITY_UNRESOLVED"
        base["freshness_status"] = "UNBOUND"
    elif claimed_workstream != workstream or claimed_role != role:
        base["result_state"] = "STRUCTURED_HANDOFF_UNBOUND"
        base["freshness_status"] = "UNBOUND"
        workstream_mismatch = claimed_workstream != workstream
        role_mismatch = claimed_role != role
        if workstream_mismatch and role_mismatch:
            mismatch = "ROLE_AND_WORKSTREAM_MISMATCH"
        elif workstream_mismatch:
            mismatch = "WORKSTREAM_MISMATCH"
        else:
            mismatch = "ROLE_MISMATCH"
        base["handoff_identity_mismatch"] = mismatch
        base["handoff_claimed_role"] = _bounded(claimed_role, 80)
        base["handoff_expected_role"] = _bounded(role, 80)
        base["handoff_claimed_workstream"] = _bounded(claimed_workstream, 500)
        base["handoff_expected_workstream"] = _bounded(workstream, 500)
    elif role in {"REVIEWER", "ASSURANCE"}:
        reviewed = str(candidate.get("reviewed_sha") or "") or None
        if current_sha and reviewed != current_sha:
            base["result_state"] = "REVIEWED_SHA_MISMATCH"
            base["freshness_status"] = "STALE_AFTER_CANDIDATE_MOVEMENT" if reviewed else "UNBOUND"
    elif role == "WRITER":
        claimed = str(candidate.get("candidate_sha") or "") or None
        if current_sha and claimed and claimed != current_sha:
            base["result_state"] = "RESULT_STALE_AFTER_CANDIDATE_MOVEMENT"
            base["freshness_status"] = "STALE_AFTER_CANDIDATE_MOVEMENT"
    base["result_fingerprint"] = _fingerprint(base)
    return base


def materialize_project_results(project_snapshot: Mapping[str, Any], store: Any) -> dict[str, Any]:
    project = str(project_snapshot.get("project") or "")
    route = str(project_snapshot.get("route") or project)
    repository = str(project_snapshot.get("repository") or "")
    index = lineage_index(store, project=project, route=route)
    output = dict(project_snapshot)
    sessions = [dict(item) for item in project_snapshot.get("sessions") or [] if isinstance(item, Mapping)]
    results: list[dict[str, Any]] = []
    for entry in sessions:
        if str(entry.get("state") or "").upper() != TERMINAL_STATE:
            continue
        fp = str(entry.get("session_fingerprint") or "")
        candidate = entry.pop("_terminal_candidate", None)
        if not fp:
            entry["result_state"] = "RESULT_IDENTITY_UNRESOLVED"
            entry["classification"] = TERMINAL_ATTENTION_CLASSIFICATION
            results.append(_identity_result(project=project, route=route, repository=repository, fp=fp, reason="SESSION_FINGERPRINT_MISSING"))
            continue
        if str(entry.get("source_repository") or "").casefold() != repository.casefold() or entry.get("source_binding_proven") is not True:
            entry["result_state"] = "RESULT_IDENTITY_UNRESOLVED"
            entry["classification"] = TERMINAL_ATTENTION_CLASSIFICATION
            results.append(_identity_result(project=project, route=route, repository=repository, fp=fp, reason="SOURCE_REPOSITORY_BINDING_UNPROVEN"))
            continue
        matches = index.get(fp, [])
        if len(matches) != 1:
            entry["result_state"] = "RESULT_IDENTITY_UNRESOLVED"
            entry["classification"] = TERMINAL_ATTENTION_CLASSIFICATION
            reason = "NO_EXACT_LINEAGE_MATCH" if not matches else "MULTIPLE_EXACT_LINEAGE_MATCHES"
            results.append(_identity_result(project=project, route=route, repository=repository, fp=fp, reason=reason))
            continue
        lineage = matches[0]
        if not isinstance(candidate, Mapping) or candidate.get("structured") is not True:
            state = str((candidate or {}).get("state") or "COMPLETED_OUTPUT_UNSTRUCTURED")
            entry["result_state"] = state
            entry["classification"] = TERMINAL_ATTENTION_CLASSIFICATION
            result = {
                "schema_version": SCHEMA_VERSION,
                "project": project,
                "route": route,
                "logical_workstream": lineage["workstream"],
                "role": lineage["role"],
                "generation": lineage["generation"],
                "session_fingerprint": fp,
                "repository": repository,
                "status": "COMPLETE",
                "verdict": None,
                "finding_count": None,
                "findings": [],
                "result_state": state,
                "freshness_status": "UNADJUDICABLE",
                "safe_read_only_recovery_exists": True,
                "parent_action_required": True,
                "raw_activity_content_persisted": False,
                "raw_session_id_persisted": False,
            }
            result["result_fingerprint"] = _fingerprint(result)
            results.append(result)
            continue
        result = _bound_result(project=project, route=route, repository=repository, session=entry, candidate=candidate, lineage=lineage)
        entry["result_state"] = result["result_state"]
        entry["classification"] = "COMPLETED_OUTPUT_CONSUMED" if result["result_state"] == "PARENT_CONSUMABLE" else TERMINAL_ATTENTION_CLASSIFICATION
        results.append(result)
    counts: dict[str, int] = {}
    for entry in sessions:
        classification = str(entry.get("classification") or "UNKNOWN")
        counts[classification] = counts.get(classification, 0) + 1
    output["sessions"] = sessions
    output["results"] = results
    output["result_count"] = len(results)
    output["parent_consumable_result_count"] = sum(item.get("result_state") == "PARENT_CONSUMABLE" for item in results)
    output["classification_counts"] = dict(sorted(counts.items()))
    output["attention_required"] = any(
        item.get("classification") == TERMINAL_ATTENTION_CLASSIFICATION
        or str(item.get("classification") or "") in {
            "WAITING_INPUT_REQUIRES_RECONCILIATION",
            "TERMINAL_FAILURE_REQUIRES_RECONCILIATION",
            "PROVIDER_STATE_UNKNOWN",
            "PROVIDER_SESSION_IDENTITY_INCOMPLETE",
        }
        for item in sessions
    )
    output["terminal_result_materialization"] = "READ_ONLY_EXACT_LINEAGE_BINDING"
    output["provider_mutation_performed"] = False
    output["raw_activity_content_persisted"] = False
    return output
