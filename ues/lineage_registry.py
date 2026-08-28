from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping, Sequence

from .identity import canonical_lane_id
from .state_store import StateUnavailable, WorkstreamRuntimeRecord

SCHEMA_VERSION = "1.2"
VALID_ROLES = frozenset({"WRITER", "REVIEWER", "ASSURANCE"})
TERMINAL_SESSION_STATES = frozenset({"COMPLETED", "FAILED"})
DIRECT_CONTINUATION_STATES = frozenset({"AWAITING_USER_FEEDBACK", "IN_PROGRESS"})
OBSERVE_ONLY_STATES = frozenset({"QUEUED", "PLANNING", "AWAITING_PLAN_APPROVAL", "PAUSED"})


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def session_fingerprint(value: Any) -> str:
    return sha256(str(value or "").encode("utf-8")).hexdigest()


def normalize_role(value: Any) -> str:
    role = str(value or "").strip().upper()
    if role not in VALID_ROLES:
        raise ValueError(f"unsupported lineage role: {role or '<missing>'}")
    return role


def continuation_disposition(state: Any) -> str:
    normalized = str(state or "UNKNOWN").strip().upper()
    if normalized in DIRECT_CONTINUATION_STATES:
        return "REUSE_SAME_SESSION"
    if normalized == "AWAITING_PLAN_APPROVAL":
        return "APPROVE_PLAN_OR_RECONCILE_SAME_SESSION"
    if normalized in {"QUEUED", "PLANNING"}:
        return "WAIT_ACTIVE_SAME_SESSION"
    if normalized == "PAUSED":
        return "RECONCILE_PAUSED_SAME_SESSION"
    if normalized in TERMINAL_SESSION_STATES:
        return "TERMINAL_REPLACE_ONLY_IF_MORE_WORK_REQUIRED"
    return "RECONCILE_UNKNOWN_SESSION_STATE"


def lineage_lane_id(project: str, route: str, workstream: str, role: str) -> str:
    normalized_role = normalize_role(role)
    return canonical_lane_id(project, route, f"LINEAGE::{str(workstream).strip()}::{normalized_role}")


def _sort_key(session: Mapping[str, Any]) -> tuple[int, str]:
    state = str(session.get("normalizedState") or session.get("state") or "UNKNOWN").upper()
    active_rank = 1 if state not in TERMINAL_SESSION_STATES else 0
    return active_rank, str(session.get("updateTime") or session.get("createTime") or "").strip()


def match_lineage_session(
    sessions: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
    *,
    repository: str,
) -> dict[str, Any]:
    """Bind passive lifecycle reuse only from exact governed provider identity.

    Exact durable session fingerprint + exact repository are decisive identity.
    Provider starting branch is secondary provenance/diagnostic metadata: changed or
    missing branch metadata must never erase an otherwise exact governed binding.
    First-generation sessions not yet fingerprint-bound are reconciled separately
    from deterministic UES transition identity; branch-only adoption is prohibited.
    """

    expected_repo = str(repository or "").strip().casefold()
    provider_branch = str(policy.get("provider_starting_branch") or "").strip()
    fingerprints = {
        str(item).strip().lower()
        for item in policy.get("known_session_fingerprints") or []
        if str(item).strip()
    }

    if not fingerprints:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "UNBOUND",
            "reason": "EXACT_SESSION_FINGERPRINT_REQUIRED",
            "session": None,
            "candidate_count": 0,
        }

    candidates: list[Mapping[str, Any]] = []
    for session in sessions:
        repo = str(session.get("_source_repository") or "").strip().casefold()
        if repo != expected_repo:
            continue
        fp = str(session.get("_session_fingerprint") or "").strip().lower()
        if fp not in fingerprints:
            continue
        candidates.append(session)

    if not candidates:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "UNBOUND",
            "reason": "NO_EXACT_GOVERNED_SESSION_FINGERPRINT_MATCH",
            "session": None,
            "candidate_count": 0,
        }

    if len(candidates) > 1:
        active = [
            item
            for item in candidates
            if str(item.get("normalizedState") or item.get("state") or "").upper()
            not in TERMINAL_SESSION_STATES
        ]
        if len(active) == 1:
            candidates = active
        else:
            ordered = sorted(candidates, key=_sort_key, reverse=True)
            first_key = _sort_key(ordered[0])
            if len(ordered) > 1 and _sort_key(ordered[1]) == first_key:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "status": "AMBIGUOUS",
                    "reason": "MULTIPLE_EXACT_LINEAGE_SESSION_MATCHES",
                    "session": None,
                    "candidate_count": len(candidates),
                }
            candidates = [ordered[0]]

    session = candidates[0]
    state = str(session.get("normalizedState") or session.get("state") or "UNKNOWN").upper()
    actual_provider_branch = str(session.get("sourceStartingBranch") or "").strip()
    provider_branch_metadata_missing = bool(provider_branch and not actual_provider_branch)
    provider_branch_metadata_drift = bool(
        provider_branch
        and actual_provider_branch
        and actual_provider_branch != provider_branch
    )
    if provider_branch_metadata_drift:
        reason = "EXACT_GOVERNED_LINEAGE_BINDING_BRANCH_DRIFT"
    elif provider_branch_metadata_missing:
        reason = "EXACT_GOVERNED_LINEAGE_BINDING_BRANCH_METADATA_UNAVAILABLE"
    else:
        reason = "EXACT_GOVERNED_LINEAGE_BINDING"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PROVEN",
        "reason": reason,
        "session": session,
        "candidate_count": 1,
        "session_fingerprint": str(session.get("_session_fingerprint") or ""),
        "provider_state": state,
        "continuation_disposition": continuation_disposition(state),
        "provider_starting_branch_metadata_missing": provider_branch_metadata_missing,
        "provider_starting_branch_metadata_drift": provider_branch_metadata_drift,
        "expected_provider_starting_branch": provider_branch or None,
        "observed_provider_starting_branch": actual_provider_branch or None,
    }


def _legacy_branch_only_adoption(prior: Mapping[str, Any], record: WorkstreamRuntimeRecord) -> bool:
    """Identify only the unsafe passive-adoption shape emitted by older runtimes."""

    return bool(
        str(prior.get("replacement_reason") or "") == "INITIAL_ADOPTION"
        and str(prior.get("session_fingerprint") or "").strip()
        and not [
            item
            for item in prior.get("known_session_fingerprints") or []
            if str(item).strip()
        ]
        and not str(prior.get("initial_lineage_transition_key") or "").strip()
        and not str(prior.get("task_spec_digest") or "").strip()
        and record.unknown_write_state is None
        and record.action_in_flight is None
    )


def upsert_lineage_observation(
    store: Any,
    *,
    project: str,
    route: str,
    workstream: str,
    role: str,
    binding: Mapping[str, Any],
    policy: Mapping[str, Any],
    current_candidate_sha: str | None = None,
    current_pr_number: int | None = None,
) -> dict[str, Any]:
    role = normalize_role(role)
    lane_id = lineage_lane_id(project, route, workstream, role)
    read = store.read_workstream(lane_id)
    prior: Mapping[str, Any] = {}
    if read.status == "MISSING":
        record = WorkstreamRuntimeRecord(
            lane_id=lane_id,
            project=project,
            route=route,
            workstream_id=f"LINEAGE::{workstream}::{role}",
            activation_mode="SHADOW",
        )
        expected = 0
        previous_generation = 0
        previous_fp = None
    elif read.status == "OK" and read.record is not None:
        record = WorkstreamRuntimeRecord.from_dict(read.record.to_dict())
        expected = read.version
        prior = record.evidence_bindings or {}
        previous_generation = int(prior.get("generation") or 0)
        previous_fp = str(prior.get("session_fingerprint") or "") or None
    else:
        raise StateUnavailable(read.reason or f"lineage state unavailable: {lane_id}")

    record.activation_mode = "SHADOW"

    status = str(binding.get("status") or "UNBOUND")
    session = binding.get("session") if isinstance(binding.get("session"), Mapping) else None
    current_fp = str(binding.get("session_fingerprint") or "") or None
    generation = previous_generation
    replacement_reason: str | None = None
    revoked_legacy_fp: str | None = None
    governed_fingerprints = {
        str(item).strip().lower()
        for item in policy.get("known_session_fingerprints") or []
        if str(item).strip()
    }

    if status != "PROVEN" and previous_fp and _legacy_branch_only_adoption(prior, record):
        revoked_legacy_fp = previous_fp
        previous_generation = 0
        previous_fp = None
        generation = 0
        current_fp = None
        replacement_reason = "LEGACY_BRANCH_ONLY_ADOPTION_REVOKED"
        status = "UNBOUND"
    elif current_fp and current_fp != previous_fp:
        if (
            previous_fp is None
            and previous_generation > 0
            and current_fp.lower() in governed_fingerprints
        ):
            generation = previous_generation
            replacement_reason = "DURABLE_FINGERPRINT_REBOUND"
        else:
            generation = max(1, previous_generation + 1)
            replacement_reason = (
                "INITIAL_ADOPTION"
                if previous_fp is None
                else "PROVIDER_SESSION_GENERATION_CHANGED"
            )
    elif current_fp and generation == 0:
        generation = 1

    durable_fp = current_fp or previous_fp
    provider_state = str(binding.get("provider_state") or "UNKNOWN").upper()
    record.actor_bindings = (
        {
            role: {
                "provider": "jules",
                "proof_status": "PROVEN_EXPLICIT_LINEAGE",
                "session_fingerprint": current_fp,
                "source_repository": session.get("_source_repository") if session else None,
                "provider_starting_branch": session.get("sourceStartingBranch") if session else None,
                "raw_session_id_persisted": False,
            }
        }
        if status == "PROVEN" and current_fp
        else {}
    )
    record.authority_provenance = {
        "scope": "LOGICAL_LINEAGE_REGISTRY",
        "role": role,
        "workstream": workstream,
        "session_reuse_policy": "REUSE_SAME_SESSION_FIRST_WITH_EXACT_IDENTITY",
        "replacement_policy": "NEW_GENERATION_SAME_LOGICAL_LINEAGE_ONLY",
        "labels_or_titles_are_authority": False,
        "provider_branch_is_identity": False,
        "provider_branch_and_pr_head_are_separate_facts": True,
        "observation_resets_transient_effect_authority": True,
    }
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "role": role,
        "workstream": workstream,
        "generation": generation,
        "session_fingerprint": durable_fp,
        "previous_session_fingerprint": previous_fp,
        "replacement_reason": replacement_reason,
        "binding_status": status,
        "binding_reason": binding.get("reason"),
        "known_session_fingerprints": sorted(
            str(item)
            for item in policy.get("known_session_fingerprints") or []
            if str(item)
        ),
        "expected_provider_starting_branch": policy.get("provider_starting_branch"),
        "expected_pr_head_branch": policy.get("pr_head_branch"),
        "current_pr_number": current_pr_number,
        "current_candidate_sha": current_candidate_sha,
        "raw_session_id_persisted": False,
    }
    if binding.get("provider_starting_branch_metadata_missing") is not None:
        evidence["provider_starting_branch_metadata_missing"] = bool(
            binding.get("provider_starting_branch_metadata_missing")
        )
        evidence["observed_provider_starting_branch"] = binding.get(
            "observed_provider_starting_branch"
        )
    if binding.get("provider_starting_branch_metadata_drift") is not None:
        evidence["provider_starting_branch_metadata_drift"] = bool(
            binding.get("provider_starting_branch_metadata_drift")
        )
        evidence["observed_provider_starting_branch"] = binding.get(
            "observed_provider_starting_branch"
        )
    if revoked_legacy_fp:
        evidence["revoked_legacy_session_fingerprint"] = revoked_legacy_fp
        evidence["legacy_binding_reconciliation"] = (
            "LOCAL_BINDING_REVOKED_PROVIDER_SESSION_UNTOUCHED"
        )
    record.evidence_bindings = evidence
    record.last_observed_provider_state = {
        "state": provider_state,
        "continuation_disposition": continuation_disposition(provider_state),
        "terminal": provider_state in TERMINAL_SESSION_STATES,
        "binding_status": status,
        "observed_at": _iso_now(),
    }
    record.last_successful_transition = {
        "kind": "LINEAGE_OBSERVATION",
        "binding_status": status,
        "generation": generation,
        "at": _iso_now(),
    }
    if revoked_legacy_fp:
        record.last_successful_transition["repair"] = (
            "LEGACY_BRANCH_ONLY_ADOPTION_REVOKED"
        )
    saved = store.compare_and_swap_workstream(lane_id, expected, record)
    if saved.status != "OK" or saved.record is None:
        raise StateUnavailable(saved.reason or f"failed to persist lineage state: {lane_id}")
    return {
        "schema_version": SCHEMA_VERSION,
        "lane_id": lane_id,
        "version": saved.version,
        "role": role,
        "workstream": workstream,
        "generation": generation,
        "binding_status": status,
        "session_fingerprint": current_fp,
        "provider_state": provider_state,
        "continuation_disposition": continuation_disposition(provider_state),
        "legacy_branch_only_adoption_revoked": bool(revoked_legacy_fp),
    }
