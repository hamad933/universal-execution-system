from __future__ import annotations

import argparse
import json
import re
from typing import Any, Mapping

from . import terminal_backfill
from . import terminal_recovery as recovery
from . import terminal_results
from .jules_source_probe import repository_fingerprint
from .lineage_registry import session_fingerprint
from .live_runtime import build_live_state_store

_ALLOWED_PROJECTS = frozenset({"RP01", "RP02", "RP03", "RP04"})
_WORKSTREAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_ALIAS = re.compile(r"^sha256:[0-9a-f]{64}$")
_PERSISTED = frozenset(
    {
        "TERMINAL_RESULT_PERSISTED",
        "TERMINAL_RESULT_ALREADY_PERSISTED",
        "TERMINAL_RESULT_CONCURRENTLY_PERSISTED",
        "TERMINAL_RESULT_PERSISTED_READBACK_RECONCILED",
    }
)


def _filtered_index(indexer: Any, target_workstream: str):
    def wrapped(store: Any, *, project: str, route: str) -> dict[str, list[dict[str, Any]]]:
        index = indexer(store, project=project, route=route)
        result: dict[str, list[dict[str, Any]]] = {}
        for fingerprint, matches in index.items():
            selected = [dict(item) for item in matches if str(item.get("workstream") or "") == target_workstream]
            if selected:
                result[str(fingerprint)] = selected
        return result
    return wrapped


def _project_config(project: str) -> dict[str, str]:
    projects = recovery.load_governed_projects([project])
    if len(projects) != 1:
        raise ValueError("exact terminal readback requires one governed project adapter")
    return dict(projects[0])


def _alias_bound_lineage(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
) -> dict[str, Any] | None:
    """Return one exact durable lineage only when its provider repository is hash-aliased.

    Evidence-supplement sessions intentionally run on a private transport repository while
    their reviewed candidate belongs to the product repository. The private repository name
    must never be persisted. Its durable identity is therefore a sha256 alias. Ordinary
    product-repository lineages return None and stay on the existing generic exact path.
    """

    index = terminal_results.lineage_index(store, project=project, route=route)
    matches: list[tuple[str, dict[str, Any]]] = []
    for fingerprint, lineages in index.items():
        for lineage in lineages:
            if str(lineage.get("workstream") or "") == workstream:
                matches.append((str(fingerprint), dict(lineage)))
    if len(matches) != 1:
        return None

    fingerprint, lineage = matches[0]
    lane_id = str(lineage.get("lane_id") or "").strip()
    if not fingerprint or not lane_id:
        return None
    read = store.read_workstream(lane_id)
    if read.status != "OK" or read.record is None:
        return None
    evidence = read.record.evidence_bindings or {}
    source_alias = str(evidence.get("source_repository") or "").strip().lower()
    if not _SHA256_ALIAS.fullmatch(source_alias):
        return None
    if str(evidence.get("session_fingerprint") or "").strip().lower() != fingerprint.lower():
        return None
    return {
        "fingerprint": fingerprint.lower(),
        "lineage": lineage,
        "lane_id": lane_id,
        "source_alias": source_alias,
        "candidate_sha": str(evidence.get("current_candidate_sha") or "").strip().lower() or None,
    }


def _verified_transport_source(
    session: Mapping[str, Any],
    sources: list[Mapping[str, Any]],
    *,
    expected_alias: str,
) -> bool:
    """Verify private transport repository identity in memory and return only a boolean."""

    source_name = recovery._resource_name(session.get("sourceIdentifier"))
    if not source_name:
        return False
    candidates = [
        source
        for source in sources
        if recovery._resource_name(source.get("name")) == source_name
    ]
    if len(candidates) != 1:
        return False
    repository = str(candidates[0].get("repository") or "").strip()
    if not repository:
        return False
    try:
        observed = "sha256:" + repository_fingerprint(repository)
    except ValueError:
        return False
    return observed == expected_alias


def _provider_client() -> Any | None:
    inventory = terminal_backfill._inventory_provider_client()
    activity = terminal_backfill._backfill_provider_client()
    if inventory is None or activity is None:
        return None
    return terminal_backfill._InventorySnapshotRetryClient(
        inventory,
        attempts=terminal_backfill._inventory_snapshot_attempts(),
        activity_delegate=activity,
    )


def _alias_exact_readback(
    project_cfg: Mapping[str, str],
    binding: Mapping[str, Any],
    *,
    store: Any,
    client: Any | None = None,
) -> dict[str, Any]:
    """Consume one exact hash-aliased provider session without exposing its raw source identity."""

    live_client = client or _provider_client()
    if live_client is None:
        return {
            "schema_version": "1.0",
            "result": "EXACT_TERMINAL_PROVIDER_READ_UNAVAILABLE",
            "error_category": "JULES_API_KEY_MISSING",
            "provider_read_started": False,
            "provider_read_complete": False,
            "state_persistence_complete": True,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "safe_to_blind_retry": False,
        }

    try:
        sources = list(live_client.list_sources(page_size=100))
        sessions = list(live_client.list_sessions(page_size=100))
    except recovery._READ_ERRORS as exc:
        return {
            "schema_version": "1.0",
            "result": "EXACT_TERMINAL_PROVIDER_READ_UNAVAILABLE",
            "error_category": recovery._error_category(exc),
            "provider_read_started": True,
            "provider_read_complete": False,
            "state_persistence_complete": True,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "safe_to_blind_retry": False,
        }

    wanted = str(binding.get("fingerprint") or "").lower()
    matched: list[Mapping[str, Any]] = []
    for session in sessions:
        session_name = recovery._resource_name(session.get("name"))
        if session_name and session_fingerprint(session_name).lower() == wanted:
            matched.append(session)
    if len(matched) != 1:
        return {
            "schema_version": "1.0",
            "result": "EXACT_TERMINAL_SESSION_IDENTITY_UNRESOLVED",
            "match_count": len(matched),
            "provider_read_started": True,
            "provider_read_complete": True,
            "state_persistence_complete": True,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "private_source_identity_persisted": False,
            "safe_to_blind_retry": False,
        }

    session = matched[0]
    provider_state = str(session.get("normalizedState") or session.get("state") or "UNKNOWN").upper()
    if provider_state != "COMPLETED":
        return {
            "schema_version": "1.0",
            "result": "EXACT_TERMINAL_SESSION_NOT_COMPLETED",
            "provider_state": provider_state,
            "provider_read_started": True,
            "provider_read_complete": True,
            "state_persistence_complete": True,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "private_source_identity_persisted": False,
            "safe_to_blind_retry": False,
        }

    if not _verified_transport_source(
        session,
        sources,
        expected_alias=str(binding.get("source_alias") or ""),
    ):
        return {
            "schema_version": "1.0",
            "result": "EXACT_TERMINAL_TRANSPORT_SOURCE_ALIAS_MISMATCH",
            "provider_read_started": True,
            "provider_read_complete": True,
            "state_persistence_complete": True,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "private_source_identity_persisted": False,
            "safe_to_blind_retry": False,
        }

    session_name = recovery._resource_name(session.get("name"))
    try:
        activities = live_client.list_activities(session_name, page_size=100)
    except recovery._READ_ERRORS as exc:
        return {
            "schema_version": "1.0",
            "result": "EXACT_TERMINAL_ACTIVITY_READ_UNAVAILABLE",
            "error_category": recovery._error_category(exc),
            "provider_read_started": True,
            "provider_read_complete": False,
            "state_persistence_complete": True,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "private_source_identity_persisted": False,
            "safe_to_blind_retry": False,
        }

    candidate = recovery.extract_terminal_candidate_with_legacy_recovery(activities)
    product_repository = str(project_cfg.get("repository") or "")
    project_snapshot = {
        **dict(project_cfg),
        "provider_read_complete": True,
        "provider_mutation_performed": False,
        "sessions": [
            {
                "session_fingerprint": wanted,
                "state": "COMPLETED",
                "classification": "COMPLETED_OUTPUT_REQUIRES_CONSUMPTION_CHECK",
                # This is the governed product-side identity used for result freshness.
                # The actual private transport repository was independently proven by
                # its durable hash alias above and is intentionally never copied here.
                "source_repository": product_repository,
                "source_binding_proven": True,
                "_terminal_candidate": candidate,
            }
        ],
    }
    materialized = terminal_results.materialize_project_results(project_snapshot, store)
    results = [item for item in materialized.get("results") or [] if isinstance(item, Mapping)]
    if len(results) != 1:
        return {
            "schema_version": "1.0",
            "result": "EXACT_TERMINAL_MATERIALIZATION_UNRESOLVED",
            "materialized_result_count": len(results),
            "provider_read_started": True,
            "provider_read_complete": True,
            "state_persistence_complete": True,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "private_source_identity_persisted": False,
            "safe_to_blind_retry": False,
        }

    result = results[0]
    lineage = binding.get("lineage")
    if not isinstance(lineage, Mapping):
        raise ValueError("exact alias lineage binding is unavailable")
    try:
        persistence = recovery.persist_terminal_result(store, result=result, lineage=lineage)
    except Exception as exc:
        return {
            "schema_version": "1.0",
            "result": "EXACT_TERMINAL_STATESTORE_PERSISTENCE_UNAVAILABLE",
            "error_category": recovery._error_category(exc),
            "provider_read_started": True,
            "provider_read_complete": True,
            "state_persistence_complete": False,
            "result_state": result.get("result_state"),
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_mutation_performed": False,
            "private_source_identity_persisted": False,
            "safe_to_blind_retry": False,
        }

    persisted = str(persistence.get("state") or "") in _PERSISTED
    return {
        "schema_version": "1.0",
        "result": "TERMINAL_BACKFILL_COMPLETE" if persisted else "EXACT_TERMINAL_STATESTORE_RECOVERY_REQUIRED",
        "project_count": 1,
        "projects": {
            str(project_cfg.get("project") or ""): {
                "route": project_cfg.get("route"),
                "repository": product_repository,
                "persisted_terminal_result_count": 1 if persisted else 0,
                "parent_consumable_result_count": 1 if result.get("result_state") == "PARENT_CONSUMABLE" and persisted else 0,
                "result_state_counts": {str(result.get("result_state") or "UNKNOWN"): 1},
            }
        },
        "provider_read_started": True,
        "provider_read_complete": True,
        "provider_activity_content_reads": 1,
        "provider_activity_read_failures": 0,
        "state_persistence_complete": persisted,
        "parent_consumable_result_count": 1 if result.get("result_state") == "PARENT_CONSUMABLE" and persisted else 0,
        "outcomes": [
            {
                "project": project_cfg.get("project"),
                "session_fingerprint": wanted,
                "logical_workstream": result.get("logical_workstream"),
                "role": result.get("role"),
                "generation": result.get("generation"),
                "result_state": result.get("result_state"),
                "freshness_status": result.get("freshness_status"),
                "verdict": result.get("verdict"),
                "finding_count": result.get("finding_count"),
                "persistence_state": persistence.get("state"),
                "transport_source_binding": "PROVEN_SHA256_ALIAS",
            }
        ],
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "provider_mutation_performed": False,
        "private_source_identity_persisted": False,
        "raw_session_ids_persisted": False,
        "raw_activity_content_persisted": False,
        "safe_to_blind_retry": False,
    }


def run(project: str, workstream: str) -> dict[str, Any]:
    project_id = str(project or "").strip().upper()
    target = str(workstream or "").strip()
    if project_id not in _ALLOWED_PROJECTS:
        raise ValueError("exact terminal readback project must be RP01-RP04")
    if not _WORKSTREAM.fullmatch(target):
        raise ValueError("exact terminal readback workstream is invalid")

    # Evidence-supplement transport uses a deliberately hash-aliased provider
    # repository. Consume that exact durable binding before the generic path,
    # which maps ordinary provider repositories directly to project adapters.
    try:
        store = build_live_state_store()
        project_cfg = _project_config(project_id)
        alias_binding = _alias_bound_lineage(
            store,
            project=project_id,
            route=project_cfg["route"],
            workstream=target,
        )
    except Exception:
        alias_binding = None
    if alias_binding is not None:
        result = _alias_exact_readback(project_cfg, alias_binding, store=store)
        result["exact_workstream_filter"] = target
        result["exact_workstream_readback"] = True
        result["pending_identity_reconciliation_performed"] = False
        result["dual_repository_binding_supported"] = True
        return result

    original_results_index = terminal_results.lineage_index
    original_recovery_index = recovery.lineage_index
    original_pending = recovery._pending_identity_candidates

    # Ordinary exact-workstream readback remains deliberately for one known
    # durable lineage and must not reconcile unrelated/pending identities.
    terminal_results.lineage_index = _filtered_index(original_results_index, target)
    recovery.lineage_index = _filtered_index(original_recovery_index, target)
    recovery._pending_identity_candidates = lambda store, projects: {}
    try:
        result = terminal_backfill.run([project_id])
    finally:
        terminal_results.lineage_index = original_results_index
        recovery.lineage_index = original_recovery_index
        recovery._pending_identity_candidates = original_pending

    result["exact_workstream_filter"] = target
    result["exact_workstream_readback"] = True
    result["pending_identity_reconciliation_performed"] = False
    result["dual_repository_binding_supported"] = True
    result["provider_mutation_performed"] = False
    result["new_tasks_or_sessions_created"] = 0
    result["safe_to_blind_retry"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES exact-workstream terminal-result readback")
    parser.add_argument("project", choices=sorted(_ALLOWED_PROJECTS))
    parser.add_argument("workstream")
    args = parser.parse_args(argv)
    result = run(args.project, args.workstream)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("result") == "TERMINAL_BACKFILL_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
