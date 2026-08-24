from __future__ import annotations

import json
from typing import Any

from .live_runtime import build_live_state_store
from .provider_targets import load_project_targets, provider_action
from .state_store import StateUnavailable

SCHEMA_VERSION = "2.1"


def audit_durable_provider_state(*, store: Any | None = None) -> dict[str, Any]:
    store = store or build_live_state_store()
    targets = load_project_targets()
    allowed = {(target.project, target.route, target.repository.casefold()) for target in targets}
    lanes: list[dict[str, Any]] = []
    state_counts: dict[str, dict[str, int]] = {target.project: {} for target in targets}
    classification_counts: dict[str, dict[str, int]] = {target.project: {} for target in targets}

    for lane_id in store.discover_lane_ids():
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            raise StateUnavailable(read.reason or f"provider state audit lane unavailable: {lane_id}")
        record = read.record
        if not record.workstream_id.startswith("PROVIDER-SESSION-"):
            continue
        provider = record.last_observed_provider_state or {}
        repository = str(provider.get("repository") or "")
        identity = (record.project, record.route, repository.casefold())
        if identity not in allowed:
            raise StateUnavailable("provider observation lane does not match a governed adapter")
        state = str(provider.get("state") or "UNKNOWN").upper()
        classification = str(provider.get("classification") or "")
        if classification != provider_action(state):
            raise StateUnavailable("provider observation state/classification drift")
        session_hash = str(provider.get("session_identity_hash") or "")
        if len(session_hash) != 64:
            raise StateUnavailable("provider observation session hash is invalid")
        blocked = classification != "CONTINUE_PROVIDER_OBSERVATION"
        lanes.append(
            {
                "lane_id": lane_id,
                "project": record.project,
                "route": record.route,
                "repository": repository,
                "starting_branch": provider.get("starting_branch"),
                "state": state,
                "classification": classification,
                "blocked": blocked,
                "new_waiting_activity_after_prior_user_response": bool(
                    provider.get("new_waiting_activity_after_prior_user_response")
                ),
                "session_identity_hash": session_hash,
                "raw_session_identity_emitted": False,
                "raw_message_content_emitted": False,
            }
        )
        state_counts[record.project][state] = state_counts[record.project].get(state, 0) + 1
        classification_counts[record.project][classification] = (
            classification_counts[record.project].get(classification, 0) + 1
        )

    lanes.sort(key=lambda item: (item["project"], str(item.get("starting_branch") or ""), item["session_identity_hash"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "DURABLE_PROVIDER_STATE_AUDIT",
        "provider_lane_count": len(lanes),
        "blocked_provider_lane_count": sum(1 for lane in lanes if lane["blocked"]),
        "waiting_provider_lane_count": sum(1 for lane in lanes if lane["state"] == "AWAITING_USER_FEEDBACK"),
        "new_waiting_activity_after_prior_user_response_count": sum(
            1 for lane in lanes if lane["new_waiting_activity_after_prior_user_response"]
        ),
        "project_state_counts": state_counts,
        "project_classification_counts": classification_counts,
        "lanes": lanes,
        "provider_mutation_performed": False,
        "raw_session_identity_emitted": False,
        "raw_message_content_emitted": False,
    }


def main() -> int:
    print(json.dumps(audit_durable_provider_state(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
