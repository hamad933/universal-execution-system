from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Mapping

ADAPTER_DIR = Path(__file__).resolve().parent.parent / "adapters"


@dataclass(frozen=True)
class ProjectTarget:
    project: str
    route: str
    repository: str
    adapter_id: str


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def provider_action(state: str) -> str:
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
