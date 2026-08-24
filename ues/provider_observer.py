from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping

from .identity import canonical_lane_id
from .live_runtime import build_live_state_store
from .providers.jules import JulesClient
from .state_store import StateUnavailable, WorkstreamRuntimeRecord

SCHEMA_VERSION = "1.0"
OBSERVATION_WORKSTREAM = "PROVIDER-OBSERVATION"
DEFAULT_STALE_SECONDS = 45 * 60

PROJECTS: tuple[dict[str, str], ...] = (
    {
        "project": "GS",
        "route": "GS",
        "repository": "hamad933/GS-2",
    },
    {
        "project": "CEP",
        "route": "PERSONAL:CEP",
        "repository": "hamad933/Cybersecurity-Education-Platform",
    },
)

_LABEL_PATTERNS = (
    re.compile(r"\bCEP[- ]?W\d{2}(?:[- ]?R\d{2})?\b", re.IGNORECASE),
    re.compile(r"\bCEP[- ]?AUTO[- ]?\d{3}\b", re.IGNORECASE),
    re.compile(r"\bGS[- ]?G\d+[-A-Z0-9]*\b", re.IGNORECASE),
    re.compile(r"\bW\d{2}(?:[- ]?R\d{2})?\b", re.IGNORECASE),
    re.compile(r"\bAUTO[- ]?\d{3}\b", re.IGNORECASE),
    re.compile(r"\bIPA\b", re.IGNORECASE),
)


def _env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _fingerprint(value: Any) -> str:
    return sha256(str(value or "").encode("utf-8")).hexdigest()


def _resource_name(value: Any) -> str:
    return str(value or "").strip().strip("/")


def _label_hints(title: Any) -> list[str]:
    text = str(title or "")
    result: set[str] = set()
    for pattern in _LABEL_PATTERNS:
        for match in pattern.findall(text):
            token = str(match).upper().replace(" ", "-")
            if token:
                result.add(token)
    return sorted(result)


def _role_hint(title: Any) -> str:
    text = str(title or "").upper()
    if "REVIEW" in text or "INDEPENDENT" in text or "IPA" in text:
        return "REVIEWER_OR_ASSURANCE_HEURISTIC"
    if "AUTO" in text:
        return "AUTOMATION_HEURISTIC"
    return "UNKNOWN"


def _activity_time(activity: Mapping[str, Any]) -> str | None:
    for key in (
        "createTime",
        "createdAt",
        "created_at",
        "updateTime",
        "updatedAt",
        "updated_at",
        "timestamp",
    ):
        value = activity.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _activity_type(activity: Mapping[str, Any]) -> str | None:
    for key in ("type", "activityType", "kind"):
        value = activity.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return None


def _activity_summary(activities: list[dict[str, Any]]) -> dict[str, Any]:
    canonical = json.dumps(activities, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    times = [value for item in activities if (value := _activity_time(item))]
    types = sorted({value for item in activities if (value := _activity_type(item))})
    return {
        "activity_count": len(activities),
        "activity_set_fingerprint": _fingerprint(canonical),
        "latest_activity_time": max(times) if times else None,
        "activity_types": types,
        "activity_content_persisted": False,
        "activity_ids_persisted": False,
    }


def _session_classification(state: str) -> str:
    normalized = str(state or "UNKNOWN").upper()
    if normalized == "AWAITING_USER_FEEDBACK":
        return "WAITING_INPUT_REQUIRES_RECONCILIATION"
    if normalized == "FAILED":
        return "TERMINAL_FAILURE_REQUIRES_RECONCILIATION"
    if normalized == "COMPLETED":
        return "COMPLETED_OUTPUT_REQUIRES_CONSUMPTION_CHECK"
    if normalized == "UNKNOWN":
        return "PROVIDER_STATE_UNKNOWN"
    return "ACTIVE_OR_NONTERMINAL"


def _project_for_repository(repository: str | None) -> dict[str, str] | None:
    observed = str(repository or "").strip().casefold()
    for project in PROJECTS:
        if observed == project["repository"].casefold():
            return project
    return None


def observation_lane_id(project: Mapping[str, str]) -> str:
    return canonical_lane_id(project["project"], project["route"], OBSERVATION_WORKSTREAM)


def observation_ref(project: Mapping[str, str], *, prefix: str = "ues-runtime/v2") -> str:
    lane_id = observation_lane_id(project)
    digest = sha256(lane_id.encode("utf-8")).hexdigest()
    return f"{prefix.strip('/')}/lane/{digest}"


def observation_manifest() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "JULES_PROVIDER_OBSERVATION_MANIFEST",
        "workstream": OBSERVATION_WORKSTREAM,
        "projects": [
            {
                **project,
                "lane_id": observation_lane_id(project),
                "state_ref": observation_ref(project),
            }
            for project in PROJECTS
        ],
        "raw_session_ids_persisted": False,
        "activity_content_persisted": False,
    }


def collect_provider_observation(
    client: JulesClient,
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Read Jules account state and return a sanitized GS/CEP observation snapshot.

    This function performs GET-only provider reads. Raw Jules session identifiers and
    Activity bodies are intentionally excluded from the returned/persisted snapshot.
    Exact source repository identity comes from Jules source resources.
    """

    observed_at = observed_at or _iso(_utc_now())
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

    for session in sessions:
        source_name = _resource_name(session.get("sourceIdentifier"))
        source = source_by_name.get(source_name)
        repository = source.get("repository") if isinstance(source, Mapping) else None
        project = _project_for_repository(str(repository) if repository else None)
        if project is None:
            unattributed += 1
            continue

        session_name = _resource_name(session.get("name"))
        if not session_name:
            # Provider list identity is incomplete. Preserve a fail-closed observation
            # without inventing an identity.
            projects[project["project"]]["sessions"].append(
                {
                    "session_fingerprint": None,
                    "identity_complete": False,
                    "state": str(session.get("normalizedState") or "UNKNOWN"),
                    "classification": "PROVIDER_SESSION_IDENTITY_INCOMPLETE",
                    "source_repository": project["repository"],
                    "source_binding_proven": bool(source and source.get("explicitRepositoryIdentity")),
                    "raw_session_id_persisted": False,
                    "activity_content_persisted": False,
                }
            )
            continue

        activities = client.list_activities(session_name, page_size=100)
        state = str(session.get("normalizedState") or "UNKNOWN").upper()
        title = session.get("title") or session.get("displayName") or session.get("name")
        entry = {
            "session_fingerprint": _fingerprint(session_name),
            "identity_complete": True,
            "state": state,
            "state_authoritative": bool(session.get("stateAuthoritative")),
            "classification": _session_classification(state),
            "continuation_state_capable": state in {"AWAITING_USER_FEEDBACK", "IN_PROGRESS"},
            "terminal": state in {"FAILED", "COMPLETED"},
            "source_fingerprint": _fingerprint(source_name),
            "source_repository": project["repository"],
            "source_binding_proven": bool(source and source.get("explicitRepositoryIdentity")),
            "starting_branch": session.get("sourceStartingBranch"),
            "label_hints": _label_hints(title),
            "role_hint": _role_hint(title),
            "role_hint_authority": "HEURISTIC_ONLY",
            "title_fingerprint": _fingerprint(title),
            "raw_title_persisted": False,
            "raw_session_id_persisted": False,
            **_activity_summary(activities),
        }
        projects[project["project"]]["sessions"].append(entry)

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
        "projects": projects,
    }


def _fresh_record(project: Mapping[str, str]) -> WorkstreamRuntimeRecord:
    lane_id = observation_lane_id(project)
    return WorkstreamRuntimeRecord(
        lane_id=lane_id,
        project=project["project"],
        route=project["route"],
        workstream_id=OBSERVATION_WORKSTREAM,
        activation_mode="SHADOW",
        authority_provenance={
            "scope": "READ_ONLY_PROVIDER_OBSERVATION",
            "provider": "JULES",
            "provider_mutation_authorized": False,
            "state_visibility": "PUBLIC_METADATA_MINIMIZED",
        },
        evidence_bindings={
            "session_identity_form": "SHA256_FINGERPRINT_ONLY",
            "source_repository_binding": "JULES_SOURCE_RESOURCE",
            "activity_content_persisted": False,
            "raw_session_ids_persisted": False,
        },
    )


def persist_provider_observation(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    store = build_live_state_store()
    projects = snapshot.get("projects")
    if not isinstance(projects, Mapping):
        raise ValueError("provider observation projects are missing")

    saved: list[dict[str, Any]] = []
    for project in PROJECTS:
        project_snapshot = projects.get(project["project"])
        if not isinstance(project_snapshot, Mapping):
            raise ValueError(f"provider observation missing project {project['project']}")
        lane_id = observation_lane_id(project)
        current = store.read_workstream(lane_id)
        if current.status == "MISSING":
            record = _fresh_record(project)
            expected_version = 0
        elif current.status == "OK" and current.record is not None:
            record = WorkstreamRuntimeRecord.from_dict(current.record.to_dict())
            expected_version = current.version
        else:
            raise StateUnavailable(current.reason or f"provider observation lane unavailable: {lane_id}")

        record.activation_mode = "SHADOW"
        record.authority_provenance = {
            "scope": "READ_ONLY_PROVIDER_OBSERVATION",
            "provider": "JULES",
            "provider_mutation_authorized": False,
            "state_visibility": "PUBLIC_METADATA_MINIMIZED",
        }
        record.actor_bindings = {}
        record.last_observed_provider_state = dict(project_snapshot)
        record.last_successful_transition = {
            "kind": "JULES_PROVIDER_OBSERVATION",
            "result": "READ_COMPLETE",
            "at": snapshot.get("observed_at"),
            "provider_mutation": False,
        }
        result = store.compare_and_swap_workstream(lane_id, expected_version, record)
        saved.append(
            {
                "project": project["project"],
                "lane_id": lane_id,
                "state_ref": observation_ref(project),
                "version": result.version,
                "session_count": project_snapshot.get("session_count"),
                "attention_required": project_snapshot.get("attention_required"),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "result": "JULES_PROVIDER_OBSERVATION_PERSISTED",
        "observed_at": snapshot.get("observed_at"),
        "saved": saved,
        "provider_mutation_performed": False,
        "raw_session_ids_persisted": False,
        "activity_content_persisted": False,
    }


def run_provider_observation(*, persist: bool) -> dict[str, Any]:
    client = JulesClient(_env("JULES_API_KEY"))
    snapshot = collect_provider_observation(client)
    if not persist:
        return snapshot
    persisted = persist_provider_observation(snapshot)
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "JULES_PROVIDER_OBSERVATION_COMPLETE",
        "snapshot": snapshot,
        "persistence": persisted,
        "provider_mutation_performed": False,
    }


def audit_provider_observation(
    *,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    if stale_seconds <= 0:
        raise ValueError("stale_seconds must be positive")
    now = now or _utc_now()
    store = build_live_state_store()
    projects: list[dict[str, Any]] = []
    hard_incidents: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []

    for project in PROJECTS:
        lane_id = observation_lane_id(project)
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            incident = {
                "project": project["project"],
                "lane_id": lane_id,
                "code": "PROVIDER_OBSERVATION_UNAVAILABLE",
                "status": read.status,
            }
            hard_incidents.append(incident)
            projects.append({**incident, "attention_required": True})
            continue

        provider_state = read.record.last_observed_provider_state
        if not isinstance(provider_state, Mapping):
            incident = {
                "project": project["project"],
                "lane_id": lane_id,
                "code": "PROVIDER_OBSERVATION_MISSING",
            }
            hard_incidents.append(incident)
            projects.append({**incident, "attention_required": True})
            continue

        observed_at = str(provider_state.get("observed_at") or "")
        stale = True
        age_seconds: float | None = None
        try:
            age_seconds = max(0.0, (now - _parse_time(observed_at)).total_seconds())
            stale = age_seconds > stale_seconds
        except (TypeError, ValueError):
            stale = True
        if stale:
            hard_incidents.append(
                {
                    "project": project["project"],
                    "lane_id": lane_id,
                    "code": "PROVIDER_OBSERVATION_STALE",
                    "age_seconds": age_seconds,
                }
            )

        sessions = provider_state.get("sessions")
        session_list = sessions if isinstance(sessions, list) else []
        project_attention = [
            {
                "session_fingerprint": item.get("session_fingerprint"),
                "classification": item.get("classification"),
                "state": item.get("state"),
                "label_hints": item.get("label_hints") or [],
                "role_hint": item.get("role_hint"),
            }
            for item in session_list
            if isinstance(item, Mapping)
            and item.get("classification")
            in {
                "WAITING_INPUT_REQUIRES_RECONCILIATION",
                "TERMINAL_FAILURE_REQUIRES_RECONCILIATION",
                "COMPLETED_OUTPUT_REQUIRES_CONSUMPTION_CHECK",
                "PROVIDER_STATE_UNKNOWN",
                "PROVIDER_SESSION_IDENTITY_INCOMPLETE",
            }
        ]
        if project_attention:
            attention.append(
                {
                    "project": project["project"],
                    "lane_id": lane_id,
                    "sessions": project_attention,
                }
            )
        projects.append(
            {
                "project": project["project"],
                "lane_id": lane_id,
                "state_ref": observation_ref(project),
                "version": read.version,
                "observed_at": observed_at,
                "age_seconds": age_seconds,
                "stale": stale,
                "session_count": provider_state.get("session_count"),
                "classification_counts": provider_state.get("classification_counts") or {},
                "attention_required": bool(project_attention),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "result": "JULES_PROVIDER_OBSERVATION_AUDIT",
        "cycle_status": "PROVIDER_OBSERVER_HARD_FAILURE" if hard_incidents else "PROVIDER_OBSERVER_OK",
        "hard_incidents": hard_incidents,
        "attention_required": bool(attention),
        "attention": attention,
        "projects": projects,
        "provider_mutation_performed": False,
        "raw_session_ids_persisted": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES read-only Jules project observer")
    parser.add_argument("command", choices=("preview", "observe", "audit", "manifest"))
    parser.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    args = parser.parse_args(argv)

    if args.command == "preview":
        result = run_provider_observation(persist=False)
    elif args.command == "observe":
        result = run_provider_observation(persist=True)
    elif args.command == "audit":
        result = audit_provider_observation(stale_seconds=args.stale_seconds)
    else:
        result = observation_manifest()

    print(json.dumps(result, sort_keys=True))
    if args.command == "audit" and result.get("cycle_status") != "PROVIDER_OBSERVER_OK":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
