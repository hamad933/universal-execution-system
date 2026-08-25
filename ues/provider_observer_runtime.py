from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Mapping

from .identity import canonical_lane_id
from .live_runtime import build_live_state_store
from .provider_observer import (
    PROJECTS,
    SCHEMA_VERSION,
    _activity_summary,
    _fingerprint,
    _label_hints,
    _project_for_repository,
    _resource_name,
    _role_hint,
    _session_classification,
    audit_provider_observation,
    persist_provider_observation,
)
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
from .state_store import StateUnavailable, WorkstreamRuntimeRecord
from .terminal_results import extract_terminal_candidate, lineage_index, materialize_project_results

HEALTH_WORKSTREAM = "PROVIDER-OBSERVER-HEALTH"
_DEFAULT_OBSERVER_PROJECT_SCOPE = ("CEP", "GS")
_READ_ERRORS = (
    AuthenticationError,
    AuthorizationError,
    NetworkError,
    NotFoundError,
    ProtocolError,
    RateLimitError,
    ServerError,
)
_ACTIVITY_READ_STATES = frozenset({"AWAITING_USER_FEEDBACK", "COMPLETED"})
_DEFAULT_ACTIVITY_READ_WORKERS = 4
_MAX_ACTIVITY_READ_WORKERS = 8


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _observer_project_scope() -> tuple[str, ...]:
    names = sorted(
        {
            str(project.get("project") or "").strip().upper()
            for project in PROJECTS
            if str(project.get("project") or "").strip()
        }
    )
    if not names:
        raise ValueError("provider observer project scope must not be empty")
    return tuple(names)


def _health_workstream_id() -> str:
    scope = _observer_project_scope()
    if scope == _DEFAULT_OBSERVER_PROJECT_SCOPE:
        return HEALTH_WORKSTREAM
    return f"{HEALTH_WORKSTREAM}-{'-'.join(scope)}"


def _health_lane_id() -> str:
    return canonical_lane_id("UES", "INTERNAL:UES", _health_workstream_id())


def _error_category(exc: Exception) -> str:
    value = getattr(exc, "category", None)
    return str(value or type(exc).__name__).upper()[:120]


def _activity_read_workers() -> int:
    raw = str(os.environ.get("UES_PROVIDER_ACTIVITY_READ_WORKERS") or "").strip()
    if not raw:
        return _DEFAULT_ACTIVITY_READ_WORKERS
    try:
        requested = int(raw)
    except ValueError:
        return _DEFAULT_ACTIVITY_READ_WORKERS
    return max(1, min(_MAX_ACTIVITY_READ_WORKERS, requested))


def _bound_terminal_fingerprints(store: Any) -> dict[str, frozenset[str]]:
    """Resolve durable exact-lineage session identities before terminal Activity reads."""
    result: dict[str, frozenset[str]] = {}
    for project in PROJECTS:
        project_name = str(project.get("project") or "").strip()
        if not project_name:
            continue
        route = str(project.get("route") or project_name).strip()
        index = lineage_index(store, project=project_name, route=route)
        result[project_name] = frozenset(index)
    return result


def persist_health(*, phase: str, status: str, error_category: str | None = None) -> dict[str, Any]:
    store = build_live_state_store()
    scope = _observer_project_scope()
    workstream_id = _health_workstream_id()
    lane_id = _health_lane_id()
    read = store.read_workstream(lane_id)
    if read.status == "MISSING":
        record = WorkstreamRuntimeRecord(
            lane_id=lane_id,
            project="UES",
            route="INTERNAL:UES",
            workstream_id=workstream_id,
            activation_mode="SHADOW",
        )
        expected = 0
    elif read.status == "OK" and read.record is not None:
        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        expected = read.version
    else:
        raise StateUnavailable(read.reason or "provider observer health lane unavailable")

    record.activation_mode = "SHADOW"
    record.actor_bindings = {}
    record.authority_provenance = {
        "scope": "READ_ONLY_PROVIDER_OBSERVER_HEALTH",
        "observer_project_scope": list(scope),
        "provider_mutation_authorized": False,
        "exception_text_persisted": False,
    }
    record.last_observed_provider_state = {
        "phase": phase,
        "status": status,
        "error_category": error_category,
        "observer_project_scope": list(scope),
        "provider_mutation_performed": False,
        "exception_text_persisted": False,
    }
    record.last_successful_transition = {
        "kind": "PROVIDER_OBSERVER_HEALTH",
        "phase": phase,
        "status": status,
        "observer_project_scope": list(scope),
        "at": _iso(_utc_now()),
    }
    saved = store.compare_and_swap_workstream(lane_id, expected, record)
    return {
        "lane_id": lane_id,
        "workstream_id": workstream_id,
        "observer_project_scope": list(scope),
        "version": saved.version,
        "phase": phase,
        "status": status,
        "error_category": error_category,
        "provider_mutation_performed": False,
    }


def _read_activity_candidate(
    client: JulesClient,
    session_name: str,
    state: str,
) -> dict[str, Any]:
    """Perform one independent GET-only Activity read and return sanitized materialization data."""
    try:
        activities = client.list_activities(session_name, page_size=100)
        result: dict[str, Any] = {
            "summary": _activity_summary(activities),
            "activity_read_complete": True,
            "activity_read_skipped": False,
        }
        if state == "COMPLETED":
            result["terminal_candidate"] = extract_terminal_candidate(activities)
        return result
    except _READ_ERRORS as exc:
        result = {
            "activity_read_complete": False,
            "activity_read_skipped": False,
            "activity_read_error_category": _error_category(exc),
            "exception_text_persisted": False,
        }
        if state == "COMPLETED":
            result["terminal_candidate"] = {
                "structured": False,
                "state": "COMPLETED_OUTPUT_UNCONSUMED",
            }
        return result


def collect_resilient_observation(
    client: JulesClient,
    *,
    observed_at: str | None = None,
    bound_terminal_fingerprints: Mapping[str, frozenset[str] | set[str] | tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    observed_at = observed_at or _iso(_utc_now())
    bound_terminal_fingerprints = bound_terminal_fingerprints or {}
    sources = client.list_sources(page_size=100)
    sessions = client.list_sessions(page_size=100)
    source_by_name = {
        _resource_name(source.get("name")): source
        for source in sources
        if _resource_name(source.get("name"))
    }

    projects: dict[str, dict[str, Any]] = {
        project["project"]: {
            **project,
            "observed_at": observed_at,
            "provider": "JULES",
            "provider_read_complete": True,
            "provider_mutation_performed": False,
            "sessions": [],
        }
        for project in PROJECTS
    }
    unattributed = 0
    pending_activity_reads: list[tuple[dict[str, Any], str, str]] = []

    for session in sessions:
        source_name = _resource_name(session.get("sourceIdentifier"))
        source = source_by_name.get(source_name)
        repository = source.get("repository") if isinstance(source, Mapping) else None
        project = _project_for_repository(str(repository) if repository else None)
        if project is None:
            unattributed += 1
            continue

        session_name = _resource_name(session.get("name"))
        state = str(session.get("normalizedState") or "UNKNOWN").upper()
        title = session.get("title") or session.get("displayName") or ""
        session_fingerprint = _fingerprint(session_name) if session_name else None
        entry: dict[str, Any] = {
            "session_fingerprint": session_fingerprint,
            "identity_complete": bool(session_name),
            "state": state,
            "state_authoritative": bool(session.get("stateAuthoritative")),
            "classification": (
                _session_classification(state)
                if session_name
                else "PROVIDER_SESSION_IDENTITY_INCOMPLETE"
            ),
            "continuation_state_capable": state in {"AWAITING_USER_FEEDBACK", "IN_PROGRESS"},
            "terminal": state in {"FAILED", "COMPLETED"},
            "source_repository": project["repository"],
            "source_binding_proven": bool(source and source.get("explicitRepositoryIdentity")),
            "label_hints": _label_hints(title),
            "role_hint": _role_hint(title),
            "role_hint_authority": "HEURISTIC_ONLY",
            "raw_title_persisted": False,
            "raw_session_id_persisted": False,
            "activity_content_persisted": False,
            "activity_ids_persisted": False,
            "activity_read_complete": False,
            "activity_read_skipped": state not in _ACTIVITY_READ_STATES,
        }

        completed_bound = bool(
            state == "COMPLETED"
            and session_fingerprint
            and session_fingerprint in set(bound_terminal_fingerprints.get(project["project"], ()))
        )
        should_read_activities = state == "AWAITING_USER_FEEDBACK" or completed_bound
        if state == "COMPLETED" and not completed_bound:
            entry["activity_read_skipped"] = True
            entry["activity_read_reason"] = "NO_EXACT_DURABLE_LINEAGE_BINDING"

        if session_name and should_read_activities:
            pending_activity_reads.append((entry, session_name, state))

        projects[project["project"]]["sessions"].append(entry)

    if pending_activity_reads:
        workers = min(_activity_read_workers(), len(pending_activity_reads))
        if workers == 1:
            outcomes = [
                _read_activity_candidate(client, session_name, state)
                for _, session_name, state in pending_activity_reads
            ]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ues-jules-activity") as executor:
                outcomes = list(
                    executor.map(
                        lambda item: _read_activity_candidate(client, item[1], item[2]),
                        pending_activity_reads,
                    )
                )
        for (entry, _, state), outcome in zip(pending_activity_reads, outcomes, strict=True):
            summary = outcome.get("summary")
            if isinstance(summary, Mapping):
                entry.update(summary)
            entry["activity_read_complete"] = bool(outcome.get("activity_read_complete"))
            entry["activity_read_skipped"] = bool(outcome.get("activity_read_skipped"))
            if outcome.get("activity_read_error_category"):
                entry["activity_read_error_category"] = outcome["activity_read_error_category"]
                entry["exception_text_persisted"] = False
            if state == "COMPLETED" and isinstance(outcome.get("terminal_candidate"), Mapping):
                entry["_terminal_candidate"] = dict(outcome["terminal_candidate"])

    for project in PROJECTS:
        summary = projects[project["project"]]
        summary["sessions"] = sorted(
            summary["sessions"],
            key=lambda item: str(item.get("session_fingerprint") or ""),
        )
        counts: dict[str, int] = {}
        for item in summary["sessions"]:
            classification = str(item.get("classification") or "UNKNOWN")
            counts[classification] = counts.get(classification, 0) + 1
        summary["session_count"] = len(summary["sessions"])
        summary["classification_counts"] = dict(sorted(counts.items()))
        summary["attention_required"] = any(
            item.get("classification")
            in {
                "WAITING_INPUT_REQUIRES_RECONCILIATION",
                "TERMINAL_FAILURE_REQUIRES_RECONCILIATION",
                "COMPLETED_OUTPUT_REQUIRES_CONSUMPTION_CHECK",
                "PROVIDER_STATE_UNKNOWN",
                "PROVIDER_SESSION_IDENTITY_INCOMPLETE",
            }
            for item in summary["sessions"]
        )
        summary["current_enumeration_is_lifetime_task_history"] = False

    return {
        "schema_version": SCHEMA_VERSION,
        "result": "JULES_PROVIDER_PROJECT_OBSERVATION",
        "observer_profile": "RESILIENT_RUNTIME_R2_TERMINAL_RESULTS",
        "observed_at": observed_at,
        "provider": "JULES",
        "provider_read_complete": True,
        "provider_mutation_performed": False,
        "account_visible_session_count": len(sessions),
        "source_count": len(sources),
        "unattributed_or_other_project_session_count": unattributed,
        "current_enumeration_is_lifetime_task_history": False,
        "raw_session_ids_persisted": False,
        "raw_titles_persisted": False,
        "activity_content_persisted": False,
        "activity_read_workers": min(_activity_read_workers(), len(pending_activity_reads)) if pending_activity_reads else 0,
        "projects": projects,
    }


def _materialize_snapshot(snapshot: Mapping[str, Any], store: Any | None = None) -> dict[str, Any]:
    store = store or build_live_state_store()
    result = dict(snapshot)
    projects = snapshot.get("projects")
    if not isinstance(projects, Mapping):
        raise StateUnavailable("provider observation projects missing before terminal materialization")
    result["projects"] = {
        str(name): materialize_project_results(project_snapshot, store)
        for name, project_snapshot in projects.items()
        if isinstance(project_snapshot, Mapping)
    }
    result["terminal_result_materialization"] = "READ_ONLY_EXACT_LINEAGE_BINDING"
    result["provider_mutation_performed"] = False
    result["raw_activity_content_persisted"] = False
    return result


def observe() -> dict[str, Any]:
    recovery_snapshot: dict[str, Any] | None = None
    try:
        persist_health(phase="START", status="IN_FLIGHT")
        key = str(os.environ.get("JULES_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("JULES_API_KEY missing")
        store = build_live_state_store()
        terminal_fingerprints = _bound_terminal_fingerprints(store)
        client = JulesClient(key)
        snapshot = collect_resilient_observation(
            client,
            bound_terminal_fingerprints=terminal_fingerprints,
        )
        snapshot = _materialize_snapshot(snapshot, store)
        recovery_snapshot = snapshot
        persistence = persist_provider_observation(snapshot)
        health = persist_health(phase="COMPLETE", status="PASS")
        return {
            "schema_version": SCHEMA_VERSION,
            "result": "JULES_PROVIDER_OBSERVATION_COMPLETE",
            "snapshot": snapshot,
            "persistence": persistence,
            "health": health,
            "provider_mutation_performed": False,
            "new_tasks_or_sessions_created": 0,
        }
    except Exception as exc:
        category = _error_category(exc)
        try:
            health = persist_health(phase="FAILED", status="FAIL", error_category=category)
        except Exception:
            health = {
                "phase": "FAILED",
                "status": "FAIL",
                "error_category": category,
                "health_persistence": "FAILED",
            }
        result = {
            "schema_version": SCHEMA_VERSION,
            "result": "JULES_PROVIDER_OBSERVATION_FAILED",
            "error_category": category,
            "exception_text_persisted": False,
            "provider_mutation_performed": False,
            "new_tasks_or_sessions_created": 0,
            "health": health,
        }
        if recovery_snapshot is not None:
            result.update({
                "provider_read_complete": True,
                "state_persistence_complete": False,
                "sanitized_recovery_snapshot": recovery_snapshot,
                "raw_activity_content_persisted": False,
                "safe_to_blind_retry": False,
            })
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES resilient live Jules provider observer")
    parser.add_argument("command", choices=("observe", "backfill", "audit"))
    parser.add_argument("--stale-seconds", type=int, default=45 * 60)
    args = parser.parse_args(argv)

    if args.command in {"observe", "backfill"}:
        result = observe()
        if args.command == "backfill":
            result["result_mode"] = "LEGACY_COMPLETED_SESSION_READ_ONLY_BACKFILL"
            result["provider_mutation_performed"] = False
            result["new_tasks_or_sessions_created"] = 0
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("result") == "JULES_PROVIDER_OBSERVATION_COMPLETE" else 2

    result = audit_provider_observation(stale_seconds=args.stale_seconds)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("cycle_status") == "PROVIDER_OBSERVER_OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
