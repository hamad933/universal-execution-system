from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from . import lifecycle_runtime as legacy
from . import lifecycle_runtime_observed as observed
from .current_authority import load_current_authority_json
from .observation_backed_health import (
    observation_backed_no_effect_eligible,
    run_observation_backed_no_effect_health,
)
from .providers.base import NetworkError, RateLimitError, ServerError
from .rp_readonly_runtime import RP_NAMES, _load_rp_adapter
from .stale_initial_lineage_reconciliation import reconcile_project_stale_initial_lineages


_PRE_EFFECT_PROVIDER_READ_OPERATIONS = frozenset({"jules.sessions.list", "jules.sessions.get"})
_PRE_EFFECT_PROVIDER_READ_ERRORS = (NetworkError, RateLimitError, ServerError)
_PROVIDER_READ_UNAVAILABLE_RESULT = "PROVIDER_READ_UNAVAILABLE_BEFORE_EFFECTS"
_STALE_INITIAL_PROVIDER_READ_UNAVAILABLE_RESULT = "STALE_INITIAL_LINEAGE_PROVIDER_READ_UNAVAILABLE"
_PROVIDER_READ_UNAVAILABLE_EXIT = 75
_DEFAULT_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS = 2
_MAX_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS = 3


def _validated_authority(adapter: dict[str, Any]) -> dict[str, Any] | None:
    actor = str(
        os.environ.get("UES_AUTHORITY_TRANSPORT_ACTOR")
        or os.environ.get("GITHUB_ACTOR")
        or ""
    ).strip()
    return load_current_authority_json(
        adapter,
        os.environ.get("UES_CURRENT_AUTHORITY_JSON"),
        transport_actor=actor,
    )


def _is_pre_effect_provider_read_failure(exc: BaseException) -> bool:
    return isinstance(exc, _PRE_EFFECT_PROVIDER_READ_ERRORS) and str(
        getattr(exc, "operation", "") or ""
    ) in _PRE_EFFECT_PROVIDER_READ_OPERATIONS


def _provider_inventory_snapshot_attempts() -> int:
    raw = str(os.environ.get("UES_RP_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS") or "").strip()
    if not raw:
        return _DEFAULT_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS
    try:
        requested = int(raw)
    except ValueError:
        return _DEFAULT_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS
    return max(1, min(_MAX_PROVIDER_INVENTORY_SNAPSHOT_ATTEMPTS, requested))


def _run_stale_initial_reconciliation_with_retry(
    adapter: dict[str, Any],
    authority: dict[str, Any] | None,
) -> dict[str, Any]:
    """Retry only the GET-only stale-initial provider inventory snapshot.

    Stale initial-lineage reconciliation never creates or retries a provider
    session. A transient provider inventory outage can therefore safely restart
    the complete GET-only snapshot before any provider effect. Unique identity
    binding, once proven, terminates the loop immediately so StateStore handoff
    cannot be replayed merely to obtain another observation.
    """

    attempt_limit = _provider_inventory_snapshot_attempts()
    attempt = 0
    while attempt < attempt_limit:
        attempt += 1
        result = dict(reconcile_project_stale_initial_lineages(adapter, authority))
        if result.get("result") != _STALE_INITIAL_PROVIDER_READ_UNAVAILABLE_RESULT:
            break
    result["provider_inventory_snapshot_attempts"] = attempt
    result["provider_inventory_snapshot_attempt_limit"] = attempt_limit
    result["provider_inventory_snapshot_retry_get_only"] = True
    return result


def _provider_read_unavailable_result(
    project: str,
    *,
    authority: dict[str, Any] | None,
    exc: BaseException,
) -> dict[str, Any]:
    """Represent a proven pre-effect provider inventory outage without guessing state.

    The RP live lifecycle finishes Jules session inventory enumeration and hydration
    before any provider mutation. Only transient failures of the exact allowlisted
    inventory operations are converted here. Other provider failures still propagate
    so a possible post-write condition can never be mislabeled as zero-effect.
    """

    return {
        "schema_version": "1.0",
        "project": project,
        "result": _PROVIDER_READ_UNAVAILABLE_RESULT,
        "lifecycle_state": "WAITING",
        "current_authority_loaded": authority is not None,
        "current_authority_event_id": (authority or {}).get("authority_event_id"),
        "provider_read_authoritative": False,
        "provider_read_operation": getattr(exc, "operation", None),
        "provider_read_error_category": getattr(exc, "category", "PROVIDER_READ_ERROR"),
        "provider_write_attempted": False,
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "retry_condition": "FRESH_AUTHORITATIVE_PROVIDER_READ_REQUIRED",
        "safe_to_blind_retry": False,
        "raw_session_ids_persisted": False,
    }


def _initial_lineage_blocked_result(lifecycle: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "project": lifecycle.get("project"),
        "result": "INITIAL_LINEAGE_RUNTIME_BLOCKED_PROVIDER_READ_UNAVAILABLE",
        "authority_event_id": lifecycle.get("current_authority_event_id"),
        "blocked_by": _PROVIDER_READ_UNAVAILABLE_RESULT,
        "provider_write_attempted": False,
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "retry_condition": "FRESH_AUTHORITATIVE_PROVIDER_READ_REQUIRED",
        "safe_to_blind_retry": False,
        "raw_session_ids_persisted": False,
    }


def run(project: str) -> dict[str, Any]:
    """Run the shared current-authority lifecycle for an RP project.

    This wrapper grants no authority. Before any effect-capable lifecycle path and
    only under the explicit owner-authorized same-repository StateStore transport,
    it performs a bounded GET-only reconciliation pass for old initial-lineage
    IN_FLIGHT/UNKNOWN operations that already have durable exact transition
    identity. That pass may bind a uniquely proven existing provider session but
    can never create/retry a provider session. Effect-capable topology still uses
    the shared live lifecycle runtime. A validated Current Authority envelope with
    no stable or dynamic lineages, no authorized initial/successor generations, and
    no authorized workflow dispatches is instead proven as a zero-effect lifecycle
    using the fresh persisted provider-observer snapshot.

    If the stale GET-only reconciliation or the effect-capable path exhausts its
    request-level retries on provider inventory hydration, the whole provider
    inventory snapshot may be retried a small bounded number of times. Stale
    reconciliation retries are admitted only because that path performs no
    provider mutation. Effect-capable retries remain admitted only for the exact
    allowlisted pre-effect provider-inventory operations (`jules.sessions.list` or
    per-session `jules.sessions.get` hydration). No other operation or possible
    post-write failure is replayed.
    """

    project_name = str(project or "").strip().upper()
    adapter = _load_rp_adapter(project_name)
    authority = _validated_authority(adapter)

    if str(os.environ.get("UES_ALLOW_PUBLIC_SAME_REPO_STATE") or "").lower() == "true":
        stale_reconciliation = _run_stale_initial_reconciliation_with_retry(
            adapter,
            authority,
        )
    else:
        stale_reconciliation = {
            "result": "STALE_INITIAL_LINEAGE_RECONCILIATION_STATESTORE_TRANSPORT_NOT_ENABLED",
            "reconciled_count": 0,
            "provider_write_attempted": False,
            "results": [],
            "provider_inventory_snapshot_attempts": 0,
            "provider_inventory_snapshot_attempt_limit": 0,
            "provider_inventory_snapshot_retry_get_only": True,
        }

    if authority is not None and observation_backed_no_effect_eligible(adapter, authority):
        result = dict(run_observation_backed_no_effect_health(adapter, authority=authority))
        result["provider_inventory_snapshot_attempts"] = 0
        result["provider_inventory_snapshot_attempt_limit"] = 0
    else:
        original_loader = legacy._load_adapter
        legacy._load_adapter = _load_rp_adapter
        attempt_limit = _provider_inventory_snapshot_attempts()
        attempt = 0
        try:
            while attempt < attempt_limit:
                attempt += 1
                try:
                    result = dict(observed.run(project_name))
                    break
                except _PRE_EFFECT_PROVIDER_READ_ERRORS as exc:
                    if not _is_pre_effect_provider_read_failure(exc):
                        raise
                    if attempt < attempt_limit:
                        continue
                    result = _provider_read_unavailable_result(
                        project_name,
                        authority=authority,
                        exc=exc,
                    )
                    break
        finally:
            legacy._load_adapter = original_loader
        result["provider_inventory_snapshot_attempts"] = attempt
        result["provider_inventory_snapshot_attempt_limit"] = attempt_limit
        result["provider_inventory_snapshot_retry_pre_effect_only"] = True

    result["project"] = project_name
    result["rp_runtime_mode"] = "CURRENT_AUTHORITY_GATED"
    result["runtime_wrapper_grants_authority"] = False
    result["stale_initial_lineage_reconciliation"] = stale_reconciliation
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES RP current-authority lifecycle wrapper")
    parser.add_argument("project", choices=sorted(RP_NAMES))
    args = parser.parse_args(argv)
    result = run(args.project)
    print(json.dumps(result, sort_keys=True))
    if result.get("result") == _PROVIDER_READ_UNAVAILABLE_RESULT:
        Path("initial-lineage-result.json").write_text(
            json.dumps(_initial_lineage_blocked_result(result), sort_keys=True),
            encoding="utf-8",
        )
        return _PROVIDER_READ_UNAVAILABLE_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
