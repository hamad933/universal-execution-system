from __future__ import annotations

import json
import re
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from .lineage_registry import session_fingerprint
from .live_runtime import build_live_state_store
from .operation_records import sanitize_receipt
from .provider_observer import _project_for_repository, _resource_name
from .providers.base import (
    AuthenticationError,
    AuthorizationError,
    NetworkError,
    NotFoundError,
    ProtocolError,
    RateLimitError,
    ServerError,
)
from .providers.jules import JulesClient
from .state_store import StateUnavailable, StateVersionConflict, WorkstreamRuntimeRecord
from .structured_handoff import END_MARKER, START_MARKER
from .terminal_results import extract_terminal_candidate, lineage_index, materialize_project_results

SCHEMA_VERSION = "1.0"
TERMINAL_RESULT_KEY = "terminal_result_v1"
HISTORICAL_TERMINAL_RESULTS_KEY = "historical_terminal_results_v1"
TERMINAL_RESULT_SCHEMA = "UES_TERMINAL_RESULT_V1"
_READ_ERRORS = (
    AuthenticationError,
    AuthorizationError,
    NetworkError,
    NotFoundError,
    ProtocolError,
    RateLimitError,
    ServerError,
)
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(raw.encode("utf-8")).hexdigest()


def _error_category(exc: BaseException) -> str:
    value = getattr(exc, "category", None)
    return str(value or type(exc).__name__).upper()[:120]


def load_governed_projects(project_names: Sequence[str] | None = None) -> tuple[dict[str, str], ...]:
    """Load every repository-backed governed adapter present in the UES checkout.

    This intentionally discovers adapter files instead of encoding RP/GS/CEP names.
    Future CLIENT/PERSONAL/INTERNAL adapters participate automatically once they have
    a governed repository identity. Projects with no reconstructed repository (for
    example an initialization-pending route) are not guessed into provider scope.
    """

    requested = {str(item).strip().upper() for item in (project_names or ()) if str(item).strip()}
    root = Path(__file__).resolve().parents[1] / "adapters"
    projects: dict[str, dict[str, str]] = {}
    for path in sorted(root.glob("*.json")):
        if path.name == "registry.json":
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, Mapping):
            continue
        project = str(value.get("project") or "").strip().upper()
        route = str(value.get("route") or project).strip()
        repository = str(value.get("repository") or "").strip()
        if not project or not route or repository.count("/") != 1:
            continue
        if project in projects and projects[project] != {
            "project": project,
            "route": route,
            "repository": repository,
        }:
            raise ValueError(f"duplicate governed adapter identity for {project}")
        projects[project] = {"project": project, "route": route, "repository": repository}
    if requested:
        missing = sorted(requested - set(projects))
        if missing:
            raise ValueError("governed repository adapter unavailable for: " + ", ".join(missing))
        projects = {name: projects[name] for name in sorted(requested)}
    return tuple(projects[name] for name in sorted(projects))


def _agent_messages(activities: Sequence[Mapping[str, Any]]) -> list[str]:
    result: list[str] = []
    for activity in activities:
        payload = activity.get("agentMessaged")
        if isinstance(payload, Mapping) and isinstance(payload.get("agentMessage"), str):
            message = str(payload.get("agentMessage") or "")
            if message:
                result.append(message)
    return result


def extract_terminal_candidate_with_legacy_recovery(
    activities: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Recover markerless JSON handoffs without ever inferring a prose verdict.

    A legacy final Agent message is used only transiently. If it contains a valid
    handoff-shaped JSON object, that object is passed through the canonical handoff
    validator/sanitizer by wrapping it in the current marker in memory. Arbitrary
    prose is never persisted as a finding and never becomes PASS/FAIL.
    """

    current = extract_terminal_candidate(activities)
    if current.get("state") != "COMPLETED_OUTPUT_UNSTRUCTURED":
        return current

    for message in reversed(_agent_messages(activities)):
        candidates = [message.strip()]
        candidates.extend(match.group(1).strip() for match in _FENCED_JSON.finditer(message))
        for raw in candidates:
            if not raw.startswith("{") or not raw.endswith("}"):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, Mapping):
                continue
            wrapped = f"{START_MARKER}\n{json.dumps(payload, ensure_ascii=False)}\n{END_MARKER}"
            recovered = extract_terminal_candidate(
                [{"agentMessaged": {"agentMessage": wrapped}}]
            )
            if recovered.get("structured") is True:
                recovered["legacy_recovery"] = "MARKERLESS_JSON_HANDOFF_RECOVERED"
                recovered["raw_activity_content_persisted"] = False
                recovered["raw_session_id_persisted"] = False
                return recovered

    current["state"] = "COMPLETED_OUTPUT_UNSTRUCTURED_REQUIRES_PARENT_CONSUMPTION"
    current["legacy_recovery"] = "NO_SAFE_STRUCTURED_PAYLOAD_FOUND"
    current["raw_activity_content_persisted"] = False
    current["raw_session_id_persisted"] = False
    return current


def _lineage_matches_result(lineage: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    return (
        str(lineage.get("role") or "").upper() == str(result.get("role") or "").upper()
        and str(lineage.get("workstream") or "") == str(result.get("logical_workstream") or "")
        and int(lineage.get("generation") or 0) == int(result.get("generation") or 0)
        and str(result.get("session_fingerprint") or "") != ""
    )


def _public_safe_result(result: Mapping[str, Any], *, lane_id: str) -> dict[str, Any]:
    safe = sanitize_receipt({key: value for key, value in result.items() if not str(key).startswith("_")})
    safe["schema_version"] = TERMINAL_RESULT_SCHEMA
    safe["lane_id"] = lane_id
    safe["raw_activity_content_persisted"] = False
    safe["raw_session_id_persisted"] = False
    safe["raw_title_persisted"] = False
    safe["secret_material_persisted"] = False
    safe["safe_to_blind_retry"] = False
    return safe


def _expected_identity(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_fingerprint": str(result.get("session_fingerprint") or "").strip().lower(),
        "role": str(result.get("role") or "").upper(),
        "workstream": str(result.get("logical_workstream") or ""),
        "generation": int(result.get("generation") or 0),
    }


def _current_identity_matches(evidence: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return (
        str(evidence.get("session_fingerprint") or "").strip().lower() == expected["session_fingerprint"]
        and str(evidence.get("role") or "").upper() == expected["role"]
        and str(evidence.get("workstream") or "") == expected["workstream"]
        and int(evidence.get("generation") or 0) == expected["generation"]
    )


def _canonical_exact_lineage_proven(
    store: Any,
    *,
    record: WorkstreamRuntimeRecord,
    lane_id: str,
    expected: Mapping[str, Any],
) -> bool:
    """Re-prove one historical identity from the canonical lineage index before write."""

    if not record.project or not record.route or not expected["session_fingerprint"]:
        return False
    matches = lineage_index(store, project=record.project, route=record.route).get(
        expected["session_fingerprint"], []
    )
    exact = [
        item
        for item in matches
        if str(item.get("lane_id") or "") == lane_id
        and str(item.get("role") or "").upper() == expected["role"]
        and str(item.get("workstream") or "") == expected["workstream"]
        and int(item.get("generation") or 0) == expected["generation"]
    ]
    return len(exact) == 1


def _historical_identity_key(expected: Mapping[str, Any]) -> str:
    return f'{int(expected["generation"])}:{expected["session_fingerprint"]}'


def _historical_entry(
    evidence: Mapping[str, Any],
    identity_key: str,
) -> Mapping[str, Any] | None:
    history = evidence.get(HISTORICAL_TERMINAL_RESULTS_KEY)
    if not isinstance(history, Mapping):
        return None
    entry = history.get(identity_key)
    return entry if isinstance(entry, Mapping) else None


def _persist_historical_terminal_result(
    store: Any,
    *,
    read: Any,
    record: WorkstreamRuntimeRecord,
    evidence: dict[str, Any],
    lane_id: str,
    expected: Mapping[str, Any],
    result: Mapping[str, Any],
    lineage: Mapping[str, Any],
) -> dict[str, Any]:
    if not _canonical_exact_lineage_proven(
        store,
        record=record,
        lane_id=lane_id,
        expected=expected,
    ):
        return {
            "state": "HISTORICAL_TERMINAL_RESULT_IDENTITY_NOT_EXACT",
            "cas_performed": False,
            "authoritative_readback": True,
            "safe_to_blind_retry": False,
        }

    safe = _public_safe_result(result, lane_id=lane_id)
    result_fp = str(safe.get("result_fingerprint") or "") or _fingerprint(safe)
    safe["result_fingerprint"] = result_fp
    safe["persistence_scope"] = "HISTORICAL_EXACT_BOUND"
    safe["identity_recovery_source"] = str(lineage.get("identity_recovery_source") or "") or None
    identity_key = _historical_identity_key(expected)

    history_raw = evidence.get(HISTORICAL_TERMINAL_RESULTS_KEY)
    if history_raw is not None and not isinstance(history_raw, Mapping):
        return {
            "state": "HISTORICAL_TERMINAL_RESULT_STORE_INVALID",
            "cas_performed": False,
            "authoritative_readback": True,
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }
    history = dict(history_raw or {})
    existing = history.get(identity_key)
    if isinstance(existing, Mapping):
        if str(existing.get("result_fingerprint") or "") == result_fp:
            return {
                "state": "HISTORICAL_TERMINAL_RESULT_ALREADY_PERSISTED",
                "cas_performed": False,
                "authoritative_readback": True,
                "version": read.version,
                "result_fingerprint": result_fp,
                "safe_to_blind_retry": False,
            }
        return {
            "state": "HISTORICAL_TERMINAL_RESULT_CONFLICT",
            "cas_performed": False,
            "authoritative_readback": True,
            "version": read.version,
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }

    history[identity_key] = safe
    evidence[HISTORICAL_TERMINAL_RESULTS_KEY] = history
    record.evidence_bindings = evidence

    try:
        saved = store.compare_and_swap_workstream(lane_id, read.version, record)
    except StateVersionConflict:
        reconciled = store.read_workstream(lane_id)
        if reconciled.status == "OK" and reconciled.record is not None:
            observed = _historical_entry(
                reconciled.record.evidence_bindings or {},
                identity_key,
            )
            if isinstance(observed, Mapping) and str(observed.get("result_fingerprint") or "") == result_fp:
                return {
                    "state": "HISTORICAL_TERMINAL_RESULT_CONCURRENTLY_PERSISTED",
                    "cas_performed": False,
                    "authoritative_readback": True,
                    "version": reconciled.version,
                    "result_fingerprint": result_fp,
                    "safe_to_blind_retry": False,
                }
        return {
            "state": "HISTORICAL_TERMINAL_RESULT_CAS_CONFLICT",
            "cas_performed": False,
            "authoritative_readback": reconciled.status == "OK",
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }
    except StateUnavailable:
        reconciled = store.read_workstream(lane_id)
        if reconciled.status == "OK" and reconciled.record is not None:
            observed = _historical_entry(
                reconciled.record.evidence_bindings or {},
                identity_key,
            )
            if isinstance(observed, Mapping) and str(observed.get("result_fingerprint") or "") == result_fp:
                return {
                    "state": "HISTORICAL_TERMINAL_RESULT_PERSISTED_READBACK_RECONCILED",
                    "cas_performed": True,
                    "authoritative_readback": True,
                    "version": reconciled.version,
                    "result_fingerprint": result_fp,
                    "safe_to_blind_retry": False,
                }
        return {
            "state": "HISTORICAL_TERMINAL_RESULT_PERSISTENCE_OUTCOME_RECONCILIATION_REQUIRED",
            "cas_performed": True,
            "authoritative_readback": False,
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }

    if saved.status != "OK" or saved.record is None:
        return {
            "state": "HISTORICAL_TERMINAL_RESULT_CAS_NOT_READABLE",
            "cas_performed": True,
            "authoritative_readback": False,
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }
    readback = store.read_workstream(lane_id)
    if readback.status != "OK" or readback.record is None:
        return {
            "state": "HISTORICAL_TERMINAL_RESULT_PERSISTED_READBACK_TEMPORARILY_UNAVAILABLE",
            "cas_performed": True,
            "authoritative_readback": False,
            "version": saved.version,
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }
    observed = _historical_entry(readback.record.evidence_bindings or {}, identity_key)
    if not isinstance(observed, Mapping) or str(observed.get("result_fingerprint") or "") != result_fp:
        return {
            "state": "HISTORICAL_TERMINAL_RESULT_READBACK_MISMATCH",
            "cas_performed": True,
            "authoritative_readback": True,
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }
    return {
        "state": "HISTORICAL_TERMINAL_RESULT_PERSISTED",
        "cas_performed": True,
        "authoritative_readback": True,
        "version": readback.version,
        "result_fingerprint": result_fp,
        "safe_to_blind_retry": False,
    }


def persist_terminal_result(
    store: Any,
    *,
    result: Mapping[str, Any],
    lineage: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist one exact-bound current or historical result with CAS and readback."""

    lane_id = str(lineage.get("lane_id") or "").strip()
    if not lane_id or not _lineage_matches_result(lineage, result):
        return {
            "state": "TERMINAL_RESULT_IDENTITY_NOT_EXACT",
            "cas_performed": False,
            "authoritative_readback": False,
            "safe_to_blind_retry": False,
        }

    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable(read.reason or "terminal result lineage unavailable before CAS")
    record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
    evidence = dict(record.evidence_bindings or {})
    expected = _expected_identity(result)

    if not _current_identity_matches(evidence, expected):
        return _persist_historical_terminal_result(
            store,
            read=read,
            record=record,
            evidence=evidence,
            lane_id=lane_id,
            expected=expected,
            result=result,
            lineage=lineage,
        )

    safe = _public_safe_result(result, lane_id=lane_id)
    result_fp = str(safe.get("result_fingerprint") or "") or _fingerprint(safe)
    safe["result_fingerprint"] = result_fp
    existing = evidence.get(TERMINAL_RESULT_KEY)
    if isinstance(existing, Mapping) and str(existing.get("result_fingerprint") or "") == result_fp:
        return {
            "state": "TERMINAL_RESULT_ALREADY_PERSISTED",
            "cas_performed": False,
            "authoritative_readback": True,
            "version": read.version,
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }

    evidence[TERMINAL_RESULT_KEY] = safe
    record.evidence_bindings = evidence
    record.last_observed_provider_state = {
        "state": "COMPLETED",
        "session_fingerprint": expected["session_fingerprint"],
        "result_state": safe.get("result_state"),
        "result_fingerprint": result_fp,
        "provider_mutation_performed": False,
        "raw_session_id_persisted": False,
        "activity_content_persisted": False,
    }
    record.last_successful_transition = {
        "kind": "TERMINAL_RESULT_MATERIALIZED",
        "result_fingerprint": result_fp,
        "result_state": safe.get("result_state"),
        "provider_mutation_performed": False,
    }

    try:
        saved = store.compare_and_swap_workstream(lane_id, read.version, record)
    except StateVersionConflict:
        reconciled = store.read_workstream(lane_id)
        if reconciled.status == "OK" and reconciled.record is not None:
            observed = (reconciled.record.evidence_bindings or {}).get(TERMINAL_RESULT_KEY)
            if isinstance(observed, Mapping) and str(observed.get("result_fingerprint") or "") == result_fp:
                return {
                    "state": "TERMINAL_RESULT_CONCURRENTLY_PERSISTED",
                    "cas_performed": False,
                    "authoritative_readback": True,
                    "version": reconciled.version,
                    "result_fingerprint": result_fp,
                    "safe_to_blind_retry": False,
                }
        return {
            "state": "TERMINAL_RESULT_CAS_CONFLICT",
            "cas_performed": False,
            "authoritative_readback": reconciled.status == "OK",
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }
    except StateUnavailable:
        reconciled = store.read_workstream(lane_id)
        if reconciled.status == "OK" and reconciled.record is not None:
            observed = (reconciled.record.evidence_bindings or {}).get(TERMINAL_RESULT_KEY)
            if isinstance(observed, Mapping) and str(observed.get("result_fingerprint") or "") == result_fp:
                return {
                    "state": "TERMINAL_RESULT_PERSISTED_READBACK_RECONCILED",
                    "cas_performed": True,
                    "authoritative_readback": True,
                    "version": reconciled.version,
                    "result_fingerprint": result_fp,
                    "safe_to_blind_retry": False,
                }
        return {
            "state": "TERMINAL_RESULT_PERSISTENCE_OUTCOME_RECONCILIATION_REQUIRED",
            "cas_performed": True,
            "authoritative_readback": False,
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }

    if saved.status != "OK" or saved.record is None:
        return {
            "state": "TERMINAL_RESULT_CAS_NOT_READABLE",
            "cas_performed": True,
            "authoritative_readback": False,
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }
    readback = store.read_workstream(lane_id)
    if readback.status != "OK" or readback.record is None:
        return {
            "state": "TERMINAL_RESULT_PERSISTED_READBACK_TEMPORARILY_UNAVAILABLE",
            "cas_performed": True,
            "authoritative_readback": False,
            "version": saved.version,
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }
    observed = (readback.record.evidence_bindings or {}).get(TERMINAL_RESULT_KEY)
    if not isinstance(observed, Mapping) or str(observed.get("result_fingerprint") or "") != result_fp:
        return {
            "state": "TERMINAL_RESULT_READBACK_MISMATCH",
            "cas_performed": True,
            "authoritative_readback": True,
            "result_fingerprint": result_fp,
            "safe_to_blind_retry": False,
        }
    return {
        "state": "TERMINAL_RESULT_PERSISTED",
        "cas_performed": True,
        "authoritative_readback": True,
        "version": readback.version,
        "result_fingerprint": result_fp,
        "safe_to_blind_retry": False,
    }


def persist_materialized_snapshot_results(snapshot: Mapping[str, Any], store: Any) -> dict[str, Any]:
    """Persist every exact-bound project result independently; one lane cannot freeze peers."""

    projects = snapshot.get("projects")
    if not isinstance(projects, Mapping):
        raise StateUnavailable("terminal recovery snapshot projects missing")
    records: list[dict[str, Any]] = []
    for project_name, project_snapshot in projects.items():
        if not isinstance(project_snapshot, Mapping):
            continue
        project = str(project_snapshot.get("project") or project_name)
        route = str(project_snapshot.get("route") or project)
        index = lineage_index(store, project=project, route=route)
        for result in project_snapshot.get("results") or []:
            if not isinstance(result, Mapping):
                continue
            fp = str(result.get("session_fingerprint") or "")
            matches = [item for item in index.get(fp, []) if _lineage_matches_result(item, result)]
            if len(matches) != 1:
                records.append({
                    "project": project,
                    "session_fingerprint": fp or None,
                    "state": "TERMINAL_RESULT_IDENTITY_UNRESOLVED",
                    "cas_performed": False,
                    "authoritative_readback": False,
                    "safe_to_blind_retry": False,
                })
                continue
            try:
                outcome = persist_terminal_result(store, result=result, lineage=matches[0])
            except StateUnavailable as exc:
                outcome = {
                    "state": "TERMINAL_RESULT_STATESTORE_UNAVAILABLE",
                    "error_category": _error_category(exc),
                    "cas_performed": False,
                    "authoritative_readback": False,
                    "safe_to_blind_retry": False,
                }
            records.append({"project": project, "session_fingerprint": fp, **outcome})
    counts = Counter(item["state"] for item in records)
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "TERMINAL_RESULT_PERSISTENCE_COMPLETE",
        "record_count": len(records),
        "state_counts": dict(sorted(counts.items())),
        "records": records,
        "provider_mutation_performed": False,
        "new_tasks_or_sessions_created": 0,
        "safe_to_blind_retry": False,
    }


def _current_view(result: Mapping[str, Any], evidence: Mapping[str, Any], repository: str) -> dict[str, Any]:
    value = dict(result)
    fp = str(value.get("session_fingerprint") or "")
    role = str(value.get("role") or "").upper()
    workstream = str(value.get("logical_workstream") or "")
    generation = int(value.get("generation") or 0)
    exact = (
        fp
        and fp == str(evidence.get("session_fingerprint") or "")
        and role == str(evidence.get("role") or "").upper()
        and workstream == str(evidence.get("workstream") or "")
        and generation == int(evidence.get("generation") or 0)
        and str(value.get("repository") or "").casefold() == repository.casefold()
    )
    if not exact:
        value["result_state"] = "RESULT_IDENTITY_UNRESOLVED"
        value["freshness_status"] = "UNBOUND"
        value["parent_action_required"] = True
    else:
        current_sha = str(evidence.get("current_candidate_sha") or "") or None
        if role in {"REVIEWER", "ASSURANCE"} and current_sha:
            reviewed = str(value.get("reviewed_sha") or "") or None
            if reviewed != current_sha:
                value["result_state"] = "REVIEWED_SHA_MISMATCH"
                value["freshness_status"] = "STALE_AFTER_CANDIDATE_MOVEMENT" if reviewed else "UNBOUND"
                value["parent_action_required"] = True
        elif role == "WRITER" and current_sha:
            candidate = str(value.get("candidate_sha") or "") or None
            if candidate and candidate != current_sha:
                value["result_state"] = "RESULT_STALE_AFTER_CANDIDATE_MOVEMENT"
                value["freshness_status"] = "STALE_AFTER_CANDIDATE_MOVEMENT"
                value["parent_action_required"] = True
    value["current_view_fingerprint"] = _fingerprint(value)
    return value


def _historical_view(
    stored: Mapping[str, Any],
    *,
    lane_id: str,
    exact_index: Mapping[str, Sequence[Mapping[str, Any]]],
    repository: str,
) -> dict[str, Any]:
    value = dict(stored)
    fp = str(value.get("session_fingerprint") or "").strip().lower()
    role = str(value.get("role") or "").upper()
    workstream = str(value.get("logical_workstream") or "")
    generation = int(value.get("generation") or 0)
    matches = [
        item
        for item in exact_index.get(fp, [])
        if str(item.get("lane_id") or "") == lane_id
        and str(item.get("role") or "").upper() == role
        and str(item.get("workstream") or "") == workstream
        and int(item.get("generation") or 0) == generation
    ]
    exact = (
        len(matches) == 1
        and str(value.get("repository") or "").casefold() == repository.casefold()
    )
    if not exact:
        value["result_state"] = "RESULT_IDENTITY_UNRESOLVED"
        value["freshness_status"] = "UNBOUND"
        value["parent_action_required"] = True
    value["persistence_scope"] = "HISTORICAL_EXACT_BOUND"
    value["current_view_fingerprint"] = _fingerprint(value)
    return value


def read_persisted_terminal_results(
    store: Any,
    *,
    project: str,
    route: str,
    repository: str,
) -> list[dict[str, Any]]:
    """Read durable current and exact-bound historical terminal results."""

    results: list[dict[str, Any]] = []
    exact_index = lineage_index(store, project=project, route=route)
    for lane_id in store.discover_lane_ids():
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            continue
        record = read.record
        if record.project != project or record.route != route:
            continue
        evidence = record.evidence_bindings or {}
        stored = evidence.get(TERMINAL_RESULT_KEY)
        if isinstance(stored, Mapping):
            view = _current_view(stored, evidence, repository)
            view["lane_id"] = lane_id
            view["persistence_version"] = read.version
            results.append(view)
        history = evidence.get(HISTORICAL_TERMINAL_RESULTS_KEY)
        if isinstance(history, Mapping):
            for historical in history.values():
                if not isinstance(historical, Mapping):
                    continue
                view = _historical_view(
                    historical,
                    lane_id=lane_id,
                    exact_index=exact_index,
                    repository=repository,
                )
                view["lane_id"] = lane_id
                view["persistence_version"] = read.version
                results.append(view)
    return sorted(
        results,
        key=lambda item: (
            str(item.get("logical_workstream") or ""),
            str(item.get("role") or ""),
            int(item.get("generation") or 0),
        ),
    )


def _pending_identity_candidates(
    store: Any,
    projects: Sequence[Mapping[str, str]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    allowed = {(p["project"], p["route"]): p for p in projects}
    result: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for lane_id in store.discover_lane_ids():
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            continue
        record = read.record
        project_cfg = allowed.get((record.project, record.route))
        if project_cfg is None:
            continue
        evidence = record.evidence_bindings or {}
        if int(evidence.get("generation") or 0) != 0 or evidence.get("session_fingerprint"):
            continue
        pending = evidence.get("pending_initial_lineage_transition")
        if not isinstance(pending, Mapping):
            continue
        repository = str(pending.get("source_repository") or "").strip()
        branch = str(pending.get("starting_branch") or "").strip()
        marker = str(pending.get("provider_title_marker") or "").strip()
        if repository.casefold() != project_cfg["repository"].casefold() or not branch or not marker:
            continue
        key = (repository.casefold(), branch, marker)
        result.setdefault(key, []).append({
            "lane_id": lane_id,
            "version": read.version,
            "record": record,
            "project": record.project,
            "route": record.route,
            "repository": repository,
            "pending": dict(pending),
        })
    return result


def _bind_pending_identity_once(
    store: Any,
    *,
    candidate: Mapping[str, Any],
    session_fp: str,
) -> dict[str, Any]:
    lane_id = str(candidate.get("lane_id") or "")
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        raise StateUnavailable(read.reason or "pending lineage unavailable for identity reconciliation")
    record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
    evidence = dict(record.evidence_bindings or {})
    pending = evidence.get("pending_initial_lineage_transition")
    expected_pending = candidate.get("pending")
    if not isinstance(pending, Mapping) or not isinstance(expected_pending, Mapping):
        return {"state": "IDENTITY_PENDING_TRANSITION_MOVED", "cas_performed": False}
    if str(pending.get("transition_key") or "") != str(expected_pending.get("transition_key") or ""):
        return {"state": "IDENTITY_PENDING_TRANSITION_MOVED", "cas_performed": False}
    if int(evidence.get("generation") or 0) != 0 or evidence.get("session_fingerprint"):
        if str(evidence.get("session_fingerprint") or "") == session_fp:
            return {"state": "IDENTITY_ALREADY_BOUND", "cas_performed": False, "authoritative_readback": True}
        return {"state": "IDENTITY_LINEAGE_ALREADY_BOUND_DIFFERENT_SESSION", "cas_performed": False}

    transition_key = str(pending.get("transition_key") or "")
    next_generation = int(pending.get("next_generation") or 1)
    role = str(evidence.get("role") or "").upper()
    workstream = str(evidence.get("workstream") or "")
    repository = str(pending.get("source_repository") or "")
    starting_branch = str(pending.get("starting_branch") or "")
    source_name = str(pending.get("source_name") or "")
    if not transition_key or next_generation <= 0 or not role or not workstream or not repository or not starting_branch:
        return {"state": "IDENTITY_PENDING_TRANSITION_INCOMPLETE", "cas_performed": False}

    evidence.pop("pending_initial_lineage_transition", None)
    evidence.update({
        "schema_version": "1.0",
        "role": role,
        "workstream": workstream,
        "generation": next_generation,
        "session_fingerprint": session_fp,
        "creation_kind": str(pending.get("creation_kind") or "INITIAL_LOGICAL_LINEAGE"),
        "source_name_fingerprint": sha256(source_name.encode("utf-8")).hexdigest() if source_name else None,
        "source_repository": repository,
        "provider_starting_branch": starting_branch,
        "initial_lineage_transition_key": transition_key,
        "generation_transition_key": transition_key,
        "task_spec_digest": pending.get("task_spec_digest"),
        "current_candidate_sha": pending.get("candidate_sha"),
        "binding_status": "PROVEN",
        "binding_reason": "EXACT_PROVIDER_REPOSITORY_BRANCH_TITLE_MARKER_RECONCILIATION",
        "raw_session_id_persisted": False,
        "raw_title_persisted": False,
    })
    record.evidence_bindings = evidence
    record.actor_bindings = {
        role: {
            "provider": "jules",
            "proof_status": "PROVEN_EXACT_TERMINAL_RECOVERY_MARKER",
            "session_fingerprint": session_fp,
            "source_repository": repository,
            "provider_starting_branch": starting_branch,
            "raw_session_id_persisted": False,
            "raw_title_persisted": False,
        }
    }
    record.unknown_write_state = None
    record.action_in_flight = None
    record.last_observed_provider_state = {
        "binding_status": "PROVEN",
        "generation": next_generation,
        "session_fingerprint": session_fp,
        "provider_starting_branch": starting_branch,
        "raw_session_id_persisted": False,
        "raw_title_persisted": False,
    }
    record.last_successful_transition = {
        "kind": "TERMINAL_RECOVERY_EXACT_IDENTITY_BOUND",
        "generation": next_generation,
        "initial_lineage_transition_key": transition_key,
        "provider_mutation_performed": False,
    }
    try:
        store.compare_and_swap_workstream(lane_id, read.version, record)
    except StateVersionConflict:
        observed = store.read_workstream(lane_id)
        if observed.status == "OK" and observed.record is not None:
            binding = observed.record.evidence_bindings or {}
            if str(binding.get("session_fingerprint") or "") == session_fp:
                return {"state": "IDENTITY_CONCURRENTLY_BOUND", "cas_performed": False, "authoritative_readback": True}
        return {"state": "IDENTITY_CAS_CONFLICT", "cas_performed": False, "authoritative_readback": observed.status == "OK"}
    except StateUnavailable:
        observed = store.read_workstream(lane_id)
        if observed.status == "OK" and observed.record is not None:
            binding = observed.record.evidence_bindings or {}
            if str(binding.get("session_fingerprint") or "") == session_fp:
                return {"state": "IDENTITY_BOUND_READBACK_RECONCILED", "cas_performed": True, "authoritative_readback": True}
        return {"state": "IDENTITY_PERSISTENCE_OUTCOME_RECONCILIATION_REQUIRED", "cas_performed": True, "authoritative_readback": False}
    observed = store.read_workstream(lane_id)
    if observed.status != "OK" or observed.record is None:
        return {"state": "IDENTITY_BOUND_READBACK_TEMPORARILY_UNAVAILABLE", "cas_performed": True, "authoritative_readback": False}
    binding = observed.record.evidence_bindings or {}
    if str(binding.get("session_fingerprint") or "") != session_fp:
        return {"state": "IDENTITY_BOUND_READBACK_MISMATCH", "cas_performed": True, "authoritative_readback": True}
    return {"state": "IDENTITY_EXACTLY_BOUND", "cas_performed": True, "authoritative_readback": True}


def run_read_only_backfill(
    project_names: Sequence[str] | None = None,
    *,
    store: Any | None = None,
    client: JulesClient | None = None,
) -> dict[str, Any]:
    """Generic, idempotent, GET-only terminal-result recovery for all governed adapters."""

    projects = load_governed_projects(project_names)
    project_by_repo = {item["repository"].casefold(): item for item in projects}
    try:
        live_store = store or build_live_state_store()
        indexes = {
            item["project"]: lineage_index(live_store, project=item["project"], route=item["route"])
            for item in projects
        }
        pending = _pending_identity_candidates(live_store, projects)
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "result": "TERMINAL_BACKFILL_STATESTORE_UNAVAILABLE_BEFORE_PROVIDER_READ",
            "error_category": _error_category(exc),
            "provider_read_started": False,
            "provider_read_complete": False,
            "state_persistence_complete": False,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "safe_to_blind_retry": False,
            "projects": [item["project"] for item in projects],
        }

    if client is None:
        import os
        key = str(os.environ.get("JULES_API_KEY") or "").strip()
        if not key:
            return {
                "schema_version": SCHEMA_VERSION,
                "result": "TERMINAL_BACKFILL_PROVIDER_READ_UNAVAILABLE",
                "error_category": "JULES_API_KEY_MISSING",
                "provider_read_started": False,
                "provider_read_complete": False,
                "state_persistence_complete": True,
                "external_effects_dispatched": 0,
                "new_tasks_or_sessions_created": 0,
                "provider_mutation_performed": False,
                "safe_to_blind_retry": False,
                "projects": [item["project"] for item in projects],
            }
        live_client: JulesClient = JulesClient(key)
    else:
        live_client = client

    try:
        sources = live_client.list_sources(page_size=100)
        sessions = live_client.list_sessions(page_size=100)
    except _READ_ERRORS as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "result": "TERMINAL_BACKFILL_PROVIDER_READ_UNAVAILABLE",
            "error_category": _error_category(exc),
            "provider_read_started": True,
            "provider_read_complete": False,
            "state_persistence_complete": True,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "safe_to_blind_retry": False,
            "projects": [item["project"] for item in projects],
        }

    source_by_name = {
        _resource_name(source.get("name")): source
        for source in sources
        if _resource_name(source.get("name"))
    }
    outcomes: list[dict[str, Any]] = []
    provider_content_reads = 0
    provider_content_read_failures = 0
    persistence_failures = 0
    identity_reconciled = 0
    unresolved_identities = 0

    for session in sessions:
        if str(session.get("normalizedState") or "").upper() != "COMPLETED":
            continue
        source = source_by_name.get(_resource_name(session.get("sourceIdentifier")))
        repository = str(source.get("repository") or "").strip() if isinstance(source, Mapping) else ""
        project = project_by_repo.get(repository.casefold())
        if project is None:
            continue
        session_name = _resource_name(session.get("name"))
        if not session_name:
            unresolved_identities += 1
            outcomes.append({
                "project": project["project"],
                "result_state": "RESULT_IDENTITY_UNRESOLVED",
                "identity_reason": "SESSION_FINGERPRINT_MISSING",
                "provider_activity_read": False,
            })
            continue
        fp = session_fingerprint(session_name)
        matches = list(indexes[project["project"]].get(fp, []))

        if len(matches) != 1:
            branch = str(session.get("sourceStartingBranch") or "").strip()
            title = str(session.get("title") or session.get("displayName") or "")
            marker_matches: list[dict[str, Any]] = []
            for (repo_key, pending_branch, marker), candidates in pending.items():
                if repo_key != repository.casefold() or pending_branch != branch or f"[{marker}]" not in title:
                    continue
                marker_matches.extend(candidates)
            if len(marker_matches) == 1:
                try:
                    identity = _bind_pending_identity_once(live_store, candidate=marker_matches[0], session_fp=fp)
                except StateUnavailable as exc:
                    identity = {"state": "IDENTITY_STATESTORE_UNAVAILABLE", "error_category": _error_category(exc)}
                if identity.get("state") in {
                    "IDENTITY_EXACTLY_BOUND",
                    "IDENTITY_ALREADY_BOUND",
                    "IDENTITY_CONCURRENTLY_BOUND",
                    "IDENTITY_BOUND_READBACK_RECONCILED",
                }:
                    identity_reconciled += 1
                    indexes[project["project"]] = lineage_index(
                        live_store, project=project["project"], route=project["route"]
                    )
                    matches = list(indexes[project["project"]].get(fp, []))
                else:
                    outcomes.append({
                        "project": project["project"],
                        "session_fingerprint": fp,
                        "result_state": "RESULT_IDENTITY_UNRESOLVED",
                        "identity_reason": identity.get("state"),
                        "provider_activity_read": False,
                    })
            if len(matches) != 1:
                unresolved_identities += 1
                if not any(item.get("session_fingerprint") == fp for item in outcomes):
                    outcomes.append({
                        "project": project["project"],
                        "session_fingerprint": fp,
                        "result_state": "RESULT_IDENTITY_UNRESOLVED",
                        "identity_reason": "NO_UNIQUE_EXACT_LINEAGE_PROOF",
                        "provider_activity_read": False,
                    })
                continue

        lineage = matches[0]
        lane_read = live_store.read_workstream(str(lineage.get("lane_id") or ""))
        if lane_read.status == "OK" and lane_read.record is not None:
            lane_evidence = lane_read.record.evidence_bindings or {}
            existing = lane_evidence.get(TERMINAL_RESULT_KEY)
            if isinstance(existing, Mapping):
                current = _current_view(existing, lane_evidence, repository)
                if (
                    current.get("result_state") == "PARENT_CONSUMABLE"
                    and int(current.get("generation") or 0) == int(lineage.get("generation") or 0)
                    and str(current.get("session_fingerprint") or "").lower() == fp.lower()
                ):
                    outcomes.append({
                        "project": project["project"],
                        "session_fingerprint": fp,
                        "logical_workstream": current.get("logical_workstream"),
                        "role": current.get("role"),
                        "generation": current.get("generation"),
                        "result_state": "PARENT_CONSUMABLE",
                        "persistence_state": "TERMINAL_RESULT_ALREADY_PERSISTED",
                        "provider_activity_read": False,
                    })
                    continue
            historical = _historical_entry(
                lane_evidence,
                _historical_identity_key({
                    "generation": int(lineage.get("generation") or 0),
                    "session_fingerprint": fp.lower(),
                }),
            )
            if isinstance(historical, Mapping) and historical.get("result_state") == "PARENT_CONSUMABLE":
                outcomes.append({
                    "project": project["project"],
                    "session_fingerprint": fp,
                    "logical_workstream": historical.get("logical_workstream"),
                    "role": historical.get("role"),
                    "generation": historical.get("generation"),
                    "result_state": "PARENT_CONSUMABLE",
                    "persistence_state": "HISTORICAL_TERMINAL_RESULT_ALREADY_PERSISTED",
                    "provider_activity_read": False,
                })
                continue

        try:
            activities = live_client.list_activities(session_name, page_size=100)
            provider_content_reads += 1
        except _READ_ERRORS as exc:
            provider_content_read_failures += 1
            outcomes.append({
                "project": project["project"],
                "session_fingerprint": fp,
                "logical_workstream": lineage.get("workstream"),
                "role": lineage.get("role"),
                "generation": lineage.get("generation"),
                "result_state": "COMPLETED_OUTPUT_UNCONSUMED",
                "error_category": _error_category(exc),
                "provider_activity_read": True,
                "provider_activity_read_complete": False,
            })
            continue

        candidate = extract_terminal_candidate_with_legacy_recovery(activities)
        project_snapshot = {
            **project,
            "provider_read_complete": True,
            "provider_mutation_performed": False,
            "sessions": [{
                "session_fingerprint": fp,
                "state": "COMPLETED",
                "classification": "COMPLETED_OUTPUT_REQUIRES_CONSUMPTION_CHECK",
                "source_repository": repository,
                "source_binding_proven": bool(source and source.get("explicitRepositoryIdentity")),
                "_terminal_candidate": candidate,
            }],
        }
        materialized = materialize_project_results(project_snapshot, live_store)
        result = materialized.get("results", [None])[0] if materialized.get("results") else None
        if not isinstance(result, Mapping):
            outcomes.append({
                "project": project["project"],
                "session_fingerprint": fp,
                "result_state": "PROVIDER_READ_COMPLETE_BUT_LIFECYCLE_RESULTS_EMPTY",
                "provider_activity_read": True,
                "provider_activity_read_complete": True,
            })
            continue
        try:
            persistence = persist_terminal_result(live_store, result=result, lineage=lineage)
        except StateUnavailable as exc:
            persistence = {
                "state": "TERMINAL_RESULT_STATESTORE_UNAVAILABLE_AFTER_PROVIDER_READ",
                "error_category": _error_category(exc),
                "authoritative_readback": False,
                "safe_to_blind_retry": False,
            }
        if persistence.get("state") not in {
            "TERMINAL_RESULT_PERSISTED",
            "TERMINAL_RESULT_ALREADY_PERSISTED",
            "TERMINAL_RESULT_CONCURRENTLY_PERSISTED",
            "TERMINAL_RESULT_PERSISTED_READBACK_RECONCILED",
            "HISTORICAL_TERMINAL_RESULT_PERSISTED",
            "HISTORICAL_TERMINAL_RESULT_ALREADY_PERSISTED",
            "HISTORICAL_TERMINAL_RESULT_CONCURRENTLY_PERSISTED",
            "HISTORICAL_TERMINAL_RESULT_PERSISTED_READBACK_RECONCILED",
        }:
            persistence_failures += 1
        outcomes.append({
            "project": project["project"],
            "session_fingerprint": fp,
            "logical_workstream": result.get("logical_workstream"),
            "role": result.get("role"),
            "generation": result.get("generation"),
            "result_state": result.get("result_state"),
            "freshness_status": result.get("freshness_status"),
            "finding_count": result.get("finding_count"),
            "legacy_recovery": candidate.get("legacy_recovery"),
            "persistence_state": persistence.get("state"),
            "authoritative_readback": persistence.get("authoritative_readback"),
            "provider_activity_read": True,
            "provider_activity_read_complete": True,
        })

    project_summaries: dict[str, dict[str, Any]] = {}
    consumable_total = 0
    for project in projects:
        try:
            persisted = read_persisted_terminal_results(
                live_store,
                project=project["project"],
                route=project["route"],
                repository=project["repository"],
            )
        except StateUnavailable:
            persisted = []
        consumable = sum(item.get("result_state") == "PARENT_CONSUMABLE" for item in persisted)
        consumable_total += consumable
        counts = Counter(str(item.get("result_state") or "UNKNOWN") for item in persisted)
        project_summaries[project["project"]] = {
            "route": project["route"],
            "repository": project["repository"],
            "persisted_terminal_result_count": len(persisted),
            "parent_consumable_result_count": consumable,
            "result_state_counts": dict(sorted(counts.items())),
        }

    state_persistence_complete = persistence_failures == 0
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "TERMINAL_BACKFILL_COMPLETE" if state_persistence_complete else "TERMINAL_BACKFILL_PARTIAL_STATESTORE_RECOVERY_REQUIRED",
        "project_count": len(projects),
        "projects": project_summaries,
        "provider_read_started": True,
        "provider_read_complete": True,
        "provider_activity_content_reads": provider_content_reads,
        "provider_activity_read_failures": provider_content_read_failures,
        "state_persistence_complete": state_persistence_complete,
        "identity_reconciled_count": identity_reconciled,
        "unresolved_identity_count": unresolved_identities,
        "parent_consumable_result_count": consumable_total,
        "outcomes": outcomes,
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "provider_mutation_performed": False,
        "raw_session_ids_persisted": False,
        "raw_titles_persisted": False,
        "raw_activity_content_persisted": False,
        "safe_to_blind_retry": False,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="UES generic read-only terminal result backfill")
    parser.add_argument("projects", nargs="*", help="optional governed project IDs; omit for all adapter-backed projects")
    args = parser.parse_args(argv)
    result = run_read_only_backfill(args.projects or None)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("result") == "TERMINAL_BACKFILL_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
