from __future__ import annotations

import json
import os
from typing import Any

from .provider_observer import ProjectTarget, _digest, _provider_action, load_project_targets
from .providers.jules import JulesClient

SCHEMA_VERSION = "2.1"


def _required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _source_repository_map(client: JulesClient) -> dict[str, str]:
    result: dict[str, str] = {}
    for source in client.list_sources(page_size=100):
        name = str(source.get("name") or "").strip()
        repository = source.get("repository")
        explicit = bool(source.get("explicitRepositoryIdentity"))
        if name and explicit and isinstance(repository, str) and repository:
            result[name] = repository
    return result


def inventory_provider_sessions(
    *,
    client: JulesClient | None = None,
    targets: tuple[ProjectTarget, ...] | None = None,
) -> dict[str, Any]:
    client = client or JulesClient(_required_env("JULES_API_KEY"))
    targets = targets or load_project_targets()
    target_by_repo = {target.repository.casefold(): target for target in targets}
    sessions = client.list_sessions(page_size=100)
    source_repositories = _source_repository_map(client)

    observations: list[dict[str, Any]] = []
    state_counts: dict[str, dict[str, int]] = {target.project: {} for target in targets}
    classification_counts: dict[str, dict[str, int]] = {target.project: {} for target in targets}
    unbound_source = 0
    outside_monitored_repositories = 0

    for session in sessions:
        source_name = str(session.get("sourceIdentifier") or "").strip()
        repository = source_repositories.get(source_name)
        if repository is None:
            unbound_source += 1
            continue
        target = target_by_repo.get(repository.casefold())
        if target is None:
            outside_monitored_repositories += 1
            continue

        session_name = str(session.get("name") or "").strip()
        if not session_name:
            continue
        state = str(session.get("normalizedState") or "UNKNOWN").upper()
        classification = _provider_action(state)
        title = str(session.get("title") or session.get("displayName") or "").strip()
        observation = {
            "project": target.project,
            "route": target.route,
            "repository": target.repository,
            "state": state,
            "classification": classification,
            "session_identity_hash": _digest(session_name),
            "title_digest": _digest(title) if title else None,
            "source_identity_hash": _digest(source_name),
            "raw_session_identity_emitted": False,
            "raw_title_emitted": False,
        }
        observations.append(observation)
        state_counts[target.project][state] = state_counts[target.project].get(state, 0) + 1
        classification_counts[target.project][classification] = (
            classification_counts[target.project].get(classification, 0) + 1
        )

    observations.sort(key=lambda item: (item["project"], item["session_identity_hash"]))
    attention_count = sum(
        1 for item in observations if item["classification"] != "CONTINUE_PROVIDER_OBSERVATION"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "LIVE_PROVIDER_INVENTORY_PASS",
        "provider": "JULES",
        "read_only_provider_access": True,
        "provider_mutation_performed": False,
        "raw_session_identity_emitted": False,
        "raw_title_emitted": False,
        "secret_material_emitted": False,
        "provider_read_shape": "ONE_PAGINATED_SESSION_LIST_PLUS_ONE_PAGINATED_SOURCE_LIST",
        "account_session_count": len(sessions),
        "provider_source_count": len(source_repositories),
        "monitored_session_count": len(observations),
        "unbound_source_count": unbound_source,
        "outside_monitored_repository_count": outside_monitored_repositories,
        "attention_required_count": attention_count,
        "project_state_counts": state_counts,
        "project_classification_counts": classification_counts,
        "observations": observations,
    }


def main() -> int:
    print(json.dumps(inventory_provider_sessions(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
