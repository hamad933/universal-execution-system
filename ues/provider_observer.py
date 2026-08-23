from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from .identity import canonical_lane_id
from .live_runtime import build_live_state_store
from .providers.jules import JulesClient
from .state_store import StateUnavailable, WorkstreamRuntimeRecord

SCHEMA_VERSION = "2.1"
ADAPTER_DIR = Path(__file__).resolve().parent.parent / "adapters"
OBSERVATION_AUTHORITY = "UES_READ_ONLY_PROVIDER_OBSERVATION"


@dataclass(frozen=True)
class ProjectTarget:
    project: str
    route: str
    repository: str
    adapter_id: str


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def load_project_targets(adapter_dir: Path = ADAPTER_DIR) -> tuple[ProjectTarget, ...]:
    targets: list[ProjectTarget] = []
    if not adapter_dir.exists():
        raise RuntimeError(f"adapter directory not found: {adapter_dir}")
    for path in sorted(adapter_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            continue
        project = str(payload.get("project") or "").strip()
        route = str(payload.get("route") or "").strip()
        repository = str(payload.get("repository") or "").strip()
        adapter_id = str(payload.get("adapter_id") or path.stem).strip()
        truth_owners = payload.get("truth_owners")
        provider_owned = isinstance(truth_owners, Mapping) and str(
            truth_owners.get("provider_state") or ""
        ).upper() == "PROVIDER"
        if project and route and repository and provider_owned:
            targets.append(ProjectTarget(project, route, repository, adapter_id))
    repositories = [target.repository.casefold() for target in targets]
    if len(repositories) != len(set(repositories)):
        raise RuntimeError("multiple provider-observed adapters target the same repository")
    return tuple(targets)


def _provider_action(state: str) -> str:
    if state == "AWAITING_USER_FEEDBACK":
        return "CONTROLLER_INPUT_RECONCILIATION_REQUIRED"
    if state == "FAILED":
        return "TERMINAL_FAILURE_RECONCILIATION_REQUIRED"
    if state == "COMPLETED":
        return "COMPLETION_CONSUMPTION_RECONCILIATION_REQUIRED"
    if state == "PAUSED":
        return "PAUSED_SESSION_RECONCILIATION_REQUIRED"
    if state == "UNKNOWN":
        return "PROVIDER_STATE_UNCLASSIFIED"
    return "CONTINUE_PROVIDER_OBSERVATION"


def _session_name(session: Mapping[str, Any]) -> str:
    value = str(session.get("name") or "").strip()
    if not value:
        raise ValueError("provider session has no stable resource name")
    return value


def _source_repository(client: JulesClient, session: Mapping[str, Any], cache: dict[str, str | None]) -> str | None:
    source_name = session.get("sourceIdentifier")
    if not isinstance(source_name, str) or not source_name:
        return None
    if source_name not in cache:
        source = client.get_source(source_name)
        repository = source.get("repository") if source.get("explicitRepositoryIdentity") else None
        cache[source_name] = str(repository) if isinstance(repository, str) and repository else None
    return cache[source_name]


def _persist_observation(
    *,
    store: Any,
    target: ProjectTarget,
    session: Mapping[str, Any],
    repository: str,
    observed_at: str,
) -> dict[str, str]:
    session_name = _session_name(session)
    session_hash = _digest(session_name)
    workstream_id = f"PROVIDER-SESSION-{session_hash[:16].upper()}"
    lane_id = canonical_lane_id(target.project, target.route, workstream_id)
    current = store.read_workstream(lane_id)
    if current.status == "MISSING":
        record = WorkstreamRuntimeRecord(
            lane_id=lane_id,
            project=target.project,
            route=target.route,
            workstream_id=workstream_id,
            activation_mode="SHADOW",
        )
        expected_version = 0
        previous_state = None
    elif current.status == "OK" and current.record is not None:
        record = WorkstreamRuntimeRecord.from_dict(current.record.to_dict())
        expected_version = current.version
        previous = record.last_observed_provider_state or {}
        previous_state = str(previous.get("state") or "") or None
    else:
        raise StateUnavailable(current.reason or f"provider observation lane unavailable: {lane_id}")

    state = str(session.get("normalizedState") or "UNKNOWN").upper()
    action = _provider_action(state)
    title = str(session.get("title") or session.get("displayName") or "").strip()
    title_digest = _digest(title) if title else None
    source_identifier = str(session.get("sourceIdentifier") or "").strip()

    record.activation_mode = "SHADOW"
    record.actor_bindings = {
        "PROVIDER_SESSION": {
            "verification": "PROVEN_EXPLICIT_SOURCE_REPOSITORY",
            "session_identity_hash": session_hash,
            "repository": repository,
            "role": "UNCLASSIFIED_UNTIL_PROJECT_AUTHORITY_BINDS",
            "mutation_authorized": False,
        }
    }
    record.authority_provenance = {
        "authority": OBSERVATION_AUTHORITY,
        "adapter_id": target.adapter_id,
        "provider_mutation_authorized": False,
        "session_identity_persisted_raw": False,
        "secret_material_persisted": False,
    }
    record.evidence_bindings = {
        "provider": "JULES",
        "repository": repository,
        "source_identity_hash": _digest(source_identifier) if source_identifier else None,
        "session_title_digest": title_digest,
        "classification": action,
    }
    record.last_observed_provider_state = {
        "provider": "JULES",
        "state": state,
        "session_identity_hash": session_hash,
        "repository": repository,
        "observed_at": observed_at,
        "awaiting_user_feedback": state == "AWAITING_USER_FEEDBACK",
        "terminal": state in {"FAILED", "COMPLETED"},
        "classification": action,
        "raw_session_identity_persisted": False,
    }
    if previous_state != state:
        record.last_successful_transition = {
            "kind": "PROVIDER_STATE_OBSERVED",
            "from": previous_state,
            "to": state,
            "at": observed_at,
        }
    record.updated_at = observed_at
    saved = store.compare_and_swap_workstream(lane_id, expected_version, record)
    if saved.status != "OK" or saved.record is None:
        raise StateUnavailable(saved.reason or f"provider observation write failed: {lane_id}")
    return {
        "project": target.project,
        "state": state,
        "classification": action,
        "session_identity_hash": session_hash,
    }


def observe_provider_sessions(
    *,
    client: JulesClient | None = None,
    store: Any | None = None,
    targets: tuple[ProjectTarget, ...] | None = None,
) -> dict[str, Any]:
    client = client or JulesClient(_required_env("JULES_API_KEY"))
    store = store or build_live_state_store()
    targets = targets or load_project_targets()
    target_by_repo = {target.repository.casefold(): target for target in targets}
    sessions = client.list_sessions(page_size=100)
    source_cache: dict[str, str | None] = {}
    observed_at = _iso_now()

    state_counts: dict[str, dict[str, int]] = {
        target.project: {} for target in targets
    }
    classification_counts: dict[str, dict[str, int]] = {
        target.project: {} for target in targets
    }
    observed_sessions = 0
    unbound_source = 0
    outside_monitored_repositories = 0

    for session in sessions:
        repository = _source_repository(client, session, source_cache)
        if repository is None:
            unbound_source += 1
            continue
        target = target_by_repo.get(repository.casefold())
        if target is None:
            outside_monitored_repositories += 1
            continue
        item = _persist_observation(
            store=store,
            target=target,
            session=session,
            repository=repository,
            observed_at=observed_at,
        )
        observed_sessions += 1
        state_counts[target.project][item["state"]] = state_counts[target.project].get(item["state"], 0) + 1
        classification_counts[target.project][item["classification"]] = (
            classification_counts[target.project].get(item["classification"], 0) + 1
        )

    attention_count = sum(
        count
        for per_project in classification_counts.values()
        for classification, count in per_project.items()
        if classification != "CONTINUE_PROVIDER_OBSERVATION"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "LIVE_PROVIDER_INGESTION_PASS",
        "provider": "JULES",
        "read_only_provider_access": True,
        "provider_mutation_performed": False,
        "raw_session_identity_emitted": False,
        "raw_session_identity_persisted": False,
        "secret_material_persisted": False,
        "account_session_count": len(sessions),
        "observed_monitored_session_count": observed_sessions,
        "unbound_source_count": unbound_source,
        "outside_monitored_repository_count": outside_monitored_repositories,
        "attention_required_count": attention_count,
        "project_state_counts": state_counts,
        "project_classification_counts": classification_counts,
        "observed_at": observed_at,
    }


def main() -> int:
    result = observe_provider_sessions()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
