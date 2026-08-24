from __future__ import annotations

import json
import os
from typing import Any, Mapping

from .provider_observer import ProjectTarget, _digest, load_project_targets
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
        if name and source.get("explicitRepositoryIdentity") and isinstance(repository, str) and repository:
            result[name] = repository
    return result


def _activity_kind(activity: Mapping[str, Any]) -> tuple[str, str | None]:
    user = activity.get("userMessaged")
    if isinstance(user, Mapping) and isinstance(user.get("userMessage"), str):
        return "USER_MESSAGE", str(user["userMessage"])
    agent = activity.get("agentMessaged")
    if isinstance(agent, Mapping) and isinstance(agent.get("agentMessage"), str):
        return "AGENT_MESSAGE", str(agent["agentMessage"])
    return "OTHER", None


def _activity_summary(activities: list[dict[str, Any]]) -> dict[str, Any]:
    user_positions: list[int] = []
    agent_positions: list[int] = []
    user_messages: list[tuple[int, str, str | None]] = []
    agent_messages: list[tuple[int, str, str | None]] = []
    activity_kind_counts: dict[str, int] = {}

    for index, activity in enumerate(activities):
        kind, message = _activity_kind(activity)
        activity_kind_counts[kind] = activity_kind_counts.get(kind, 0) + 1
        identity = str(activity.get("name") or activity.get("id") or "").strip()
        identity_hash = _digest(identity) if identity else None
        if kind == "USER_MESSAGE" and message is not None:
            user_positions.append(index)
            user_messages.append((index, _digest(message), identity_hash))
        elif kind == "AGENT_MESSAGE" and message is not None:
            agent_positions.append(index)
            agent_messages.append((index, _digest(message), identity_hash))

    latest_user = user_messages[-1] if user_messages else None
    latest_agent = agent_messages[-1] if agent_messages else None
    latest_user_position = latest_user[0] if latest_user else None
    latest_agent_position = latest_agent[0] if latest_agent else None
    agent_after_latest_user = bool(
        latest_agent_position is not None
        and (latest_user_position is None or latest_agent_position > latest_user_position)
    )

    return {
        "activity_count": len(activities),
        "activity_kind_counts": dict(sorted(activity_kind_counts.items())),
        "provider_order_used": True,
        "latest_activity_kind": (
            _activity_kind(activities[-1])[0] if activities else None
        ),
        "latest_user_message_digest": latest_user[1] if latest_user else None,
        "latest_user_activity_hash": latest_user[2] if latest_user else None,
        "latest_agent_question_digest": latest_agent[1] if latest_agent else None,
        "latest_agent_activity_hash": latest_agent[2] if latest_agent else None,
        "agent_question_after_latest_user_message": agent_after_latest_user,
        "new_waiting_activity_after_prior_user_response": bool(
            latest_user is not None and latest_agent is not None and latest_agent[0] > latest_user[0]
        ),
        "raw_activity_identity_emitted": False,
        "raw_message_content_emitted": False,
    }


def reconcile_waiting_sessions(
    *,
    client: JulesClient | None = None,
    targets: tuple[ProjectTarget, ...] | None = None,
) -> dict[str, Any]:
    client = client or JulesClient(_required_env("JULES_API_KEY"))
    targets = targets or load_project_targets()
    target_by_repo = {target.repository.casefold(): target for target in targets}
    source_repositories = _source_repository_map(client)
    sessions = client.list_sessions(page_size=100)

    waiting: list[dict[str, Any]] = []
    for session in sessions:
        if str(session.get("normalizedState") or "UNKNOWN").upper() != "AWAITING_USER_FEEDBACK":
            continue
        source_name = str(session.get("sourceIdentifier") or "").strip()
        repository = source_repositories.get(source_name)
        if repository is None:
            continue
        target = target_by_repo.get(repository.casefold())
        if target is None:
            continue
        session_name = str(session.get("name") or "").strip()
        if not session_name:
            continue
        activities = client.list_activities(session_name, page_size=100)
        summary = _activity_summary(activities)
        waiting.append(
            {
                "project": target.project,
                "route": target.route,
                "repository": target.repository,
                "starting_branch": session.get("sourceStartingBranch"),
                "session_identity_hash": _digest(session_name),
                **summary,
            }
        )

    waiting.sort(key=lambda item: (item["project"], str(item.get("starting_branch") or "")))
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "LIVE_WAITING_ACTIVITY_RECONCILIATION_PASS",
        "provider": "JULES",
        "read_only_provider_access": True,
        "provider_mutation_performed": False,
        "waiting_session_count": len(waiting),
        "new_question_after_user_response_count": sum(
            1 for item in waiting if item["new_waiting_activity_after_prior_user_response"]
        ),
        "raw_session_identity_emitted": False,
        "raw_activity_identity_emitted": False,
        "raw_message_content_emitted": False,
        "secret_material_emitted": False,
        "waiting_sessions": waiting,
    }


def main() -> int:
    print(json.dumps(reconcile_waiting_sessions(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
