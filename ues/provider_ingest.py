from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from .identity import canonical_lane_id
from .live_runtime import build_live_state_store
from .provider_targets import ProjectTarget, load_project_targets, provider_action
from .state_store import StateUnavailable, WorkstreamRuntimeRecord

SCHEMA_VERSION = "2.1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
OBSERVATION_AUTHORITY = "UES_SANITIZED_READ_ONLY_PROVIDER_ARTIFACT"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(payload).hexdigest()


def _load(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("provider observation artifact must be a JSON object")
    return value


def _assert_sanitized_top(payload: Mapping[str, Any], *, expected_result: str) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("result") != expected_result:
        raise ValueError("unexpected provider observation artifact schema/result")
    if payload.get("provider") != "JULES" or payload.get("provider_mutation_performed") is not False:
        raise ValueError("artifact is not a read-only Jules observation")
    for field in (
        "raw_session_identity_emitted",
        "secret_material_emitted",
    ):
        if payload.get(field) is not False:
            raise ValueError(f"unsafe provider observation flag: {field}")


def _target_map(targets: tuple[ProjectTarget, ...]) -> dict[tuple[str, str, str], ProjectTarget]:
    return {
        (target.project, target.route, target.repository.casefold()): target
        for target in targets
    }


def _validated_hash(value: object, name: str) -> str:
    text = str(value or "").strip().lower()
    if not HEX64.fullmatch(text):
        raise ValueError(f"{name} must be a 64-character lowercase SHA-256 hex digest")
    return text


def _validate_identity(item: Mapping[str, Any], target_map: Mapping[tuple[str, str, str], ProjectTarget]) -> ProjectTarget:
    project = str(item.get("project") or "")
    route = str(item.get("route") or "")
    repository = str(item.get("repository") or "")
    target = target_map.get((project, route, repository.casefold()))
    if target is None:
        raise ValueError("provider observation does not match a governed adapter target")
    return target


def ingest_provider_artifacts(
    inventory: Mapping[str, Any],
    waiting: Mapping[str, Any],
    *,
    store: Any | None = None,
    targets: tuple[ProjectTarget, ...] | None = None,
) -> dict[str, Any]:
    _assert_sanitized_top(inventory, expected_result="LIVE_PROVIDER_INVENTORY_PASS")
    _assert_sanitized_top(waiting, expected_result="LIVE_WAITING_ACTIVITY_RECONCILIATION_PASS")
    if inventory.get("raw_title_emitted") is not False:
        raise ValueError("inventory must not contain raw titles")
    for field in ("raw_activity_identity_emitted", "raw_message_content_emitted"):
        if waiting.get(field) is not False:
            raise ValueError(f"waiting artifact is not sanitized: {field}")

    targets = targets or load_project_targets()
    targets_by_identity = _target_map(targets)
    waiting_by_hash: dict[str, Mapping[str, Any]] = {}
    raw_waiting = waiting.get("waiting_sessions") or []
    if not isinstance(raw_waiting, list):
        raise ValueError("waiting_sessions must be a list")
    for raw in raw_waiting:
        if not isinstance(raw, Mapping):
            raise ValueError("waiting session entry must be an object")
        _validate_identity(raw, targets_by_identity)
        session_hash = _validated_hash(raw.get("session_identity_hash"), "waiting session hash")
        if session_hash in waiting_by_hash:
            raise ValueError("duplicate waiting session hash")
        if raw.get("raw_activity_identity_emitted") is not False or raw.get("raw_message_content_emitted") is not False:
            raise ValueError("waiting session entry contains unsafe raw-data flags")
        waiting_by_hash[session_hash] = raw

    raw_inventory = inventory.get("observations") or []
    if not isinstance(raw_inventory, list):
        raise ValueError("inventory observations must be a list")
    inventory_digest = _canonical_digest(dict(inventory))
    waiting_digest = _canonical_digest(dict(waiting))
    store = store or build_live_state_store()
    persisted_at = _now()
    persisted = 0
    state_counts: dict[str, int] = {}

    for raw in raw_inventory:
        if not isinstance(raw, Mapping):
            raise ValueError("inventory observation entry must be an object")
        target = _validate_identity(raw, targets_by_identity)
        session_hash = _validated_hash(raw.get("session_identity_hash"), "session hash")
        source_hash = _validated_hash(raw.get("source_identity_hash"), "source hash")
        title_digest = raw.get("title_digest")
        if title_digest is not None:
            _validated_hash(title_digest, "title digest")
        if raw.get("raw_session_identity_emitted") is not False or raw.get("raw_title_emitted") is not False:
            raise ValueError("inventory observation entry contains unsafe raw-data flags")
        state = str(raw.get("state") or "UNKNOWN").upper()
        classification = str(raw.get("classification") or "")
        if classification != provider_action(state):
            raise ValueError("provider state/classification mismatch")
        branch = raw.get("starting_branch")
        if branch is not None and not isinstance(branch, str):
            raise ValueError("starting_branch must be a string or null")

        waiting_item = waiting_by_hash.get(session_hash)
        if waiting_item is not None:
            if state != "AWAITING_USER_FEEDBACK":
                raise ValueError("waiting artifact references a non-waiting inventory session")
            if (
                str(waiting_item.get("project") or "") != target.project
                or str(waiting_item.get("route") or "") != target.route
                or str(waiting_item.get("repository") or "").casefold() != target.repository.casefold()
            ):
                raise ValueError("waiting/inventory identity mismatch")

        workstream_id = f"PROVIDER-SESSION-{session_hash.upper()}"
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
            previous_question = None
        elif current.status == "OK" and current.record is not None:
            record = WorkstreamRuntimeRecord.from_dict(current.record.to_dict())
            expected_version = current.version
            previous = record.last_observed_provider_state or {}
            previous_state = str(previous.get("state") or "") or None
            previous_question = str(previous.get("latest_agent_question_digest") or "") or None
        else:
            raise StateUnavailable(current.reason or f"provider lane unavailable: {lane_id}")

        latest_question = None
        waiting_evidence: dict[str, Any] | None = None
        if waiting_item is not None:
            latest_question_raw = waiting_item.get("latest_agent_question_digest")
            latest_question = (
                _validated_hash(latest_question_raw, "latest agent question digest")
                if latest_question_raw
                else None
            )
            waiting_evidence = {
                "latest_activity_kind": waiting_item.get("latest_activity_kind"),
                "latest_agent_activity_hash": waiting_item.get("latest_agent_activity_hash"),
                "latest_agent_question_digest": latest_question,
                "latest_user_activity_hash": waiting_item.get("latest_user_activity_hash"),
                "latest_user_message_digest": waiting_item.get("latest_user_message_digest"),
                "new_waiting_activity_after_prior_user_response": bool(
                    waiting_item.get("new_waiting_activity_after_prior_user_response")
                ),
                "agent_question_after_latest_user_message": bool(
                    waiting_item.get("agent_question_after_latest_user_message")
                ),
                "activity_count": int(waiting_item.get("activity_count") or 0),
            }
            for key in (
                "latest_agent_activity_hash",
                "latest_user_activity_hash",
                "latest_user_message_digest",
            ):
                if waiting_evidence.get(key):
                    _validated_hash(waiting_evidence[key], key)

        record.activation_mode = "SHADOW"
        record.actor_bindings = {
            "PROVIDER_SESSION": {
                "verification": "PROVEN_EXPLICIT_SOURCE_REPOSITORY",
                "session_identity_hash": session_hash,
                "repository": target.repository,
                "starting_branch": branch,
                "role": "UNCLASSIFIED_UNTIL_PROJECT_AUTHORITY_BINDS",
                "mutation_authorized": False,
            }
        }
        record.authority_provenance = {
            "authority": OBSERVATION_AUTHORITY,
            "adapter_id": target.adapter_id,
            "provider_mutation_authorized": False,
            "provider_secret_present_in_ingest_process": False,
            "raw_session_identity_persisted": False,
            "raw_message_content_persisted": False,
        }
        record.evidence_bindings = {
            "provider": "JULES",
            "repository": target.repository,
            "starting_branch": branch,
            "source_identity_hash": source_hash,
            "session_title_digest": title_digest,
            "classification": classification,
            "inventory_snapshot_digest": inventory_digest,
            "waiting_snapshot_digest": waiting_digest,
            "waiting_evidence": waiting_evidence,
        }
        record.last_observed_provider_state = {
            "provider": "JULES",
            "state": state,
            "classification": classification,
            "session_identity_hash": session_hash,
            "repository": target.repository,
            "starting_branch": branch,
            "awaiting_user_feedback": state == "AWAITING_USER_FEEDBACK",
            "terminal": state in {"FAILED", "COMPLETED"},
            "latest_agent_question_digest": latest_question,
            "new_waiting_activity_after_prior_user_response": bool(
                waiting_evidence and waiting_evidence["new_waiting_activity_after_prior_user_response"]
            ),
            "raw_session_identity_persisted": False,
            "raw_message_content_persisted": False,
            "persisted_at": persisted_at,
        }
        if previous_state != state or previous_question != latest_question:
            record.last_successful_transition = {
                "kind": "PROVIDER_OBSERVATION_CHANGED",
                "from_state": previous_state,
                "to_state": state,
                "prior_question_digest": previous_question,
                "current_question_digest": latest_question,
                "at": persisted_at,
            }
        record.updated_at = persisted_at
        saved = store.compare_and_swap_workstream(lane_id, expected_version, record)
        if saved.status != "OK" or saved.record is None:
            raise StateUnavailable(saved.reason or f"provider lane write failed: {lane_id}")
        persisted += 1
        state_counts[state] = state_counts.get(state, 0) + 1

    if set(waiting_by_hash) - {
        _validated_hash(raw.get("session_identity_hash"), "session hash")
        for raw in raw_inventory
        if isinstance(raw, Mapping)
    }:
        raise ValueError("waiting artifact contains a session absent from inventory")

    return {
        "schema_version": SCHEMA_VERSION,
        "result": "SANITIZED_PROVIDER_STATESTORE_INGEST_PASS",
        "persisted_session_count": persisted,
        "state_counts": dict(sorted(state_counts.items())),
        "provider_secret_present_in_ingest_process": False,
        "provider_mutation_performed": False,
        "raw_session_identity_persisted": False,
        "raw_message_content_persisted": False,
        "inventory_snapshot_digest": inventory_digest,
        "waiting_snapshot_digest": waiting_digest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persist sanitized UES provider observations")
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--waiting", required=True)
    args = parser.parse_args(argv)
    result = ingest_provider_artifacts(_load(args.inventory), _load(args.waiting))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
