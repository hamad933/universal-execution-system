from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any


def _normalize_path(value: str) -> str:
    raw = value.replace("\\", "/").strip()
    if not raw:
        raise ValueError("path must not be empty")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe repository path: {value}")
    return path.as_posix()


def _matches(pattern: str, path: str) -> bool:
    pattern_parts = _normalize_path(pattern).split("/")
    path_parts = _normalize_path(path).split("/")

    @lru_cache(maxsize=None)
    def match(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        token = pattern_parts[pattern_index]
        if token == "**":
            return match(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and match(pattern_index, path_index + 1)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], token)
            and match(pattern_index + 1, path_index + 1)
        )

    return match(0, 0)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def validate_authority(
    envelope: dict[str, Any],
    *,
    repository: str,
    ref: str,
    live_head_sha: str,
    live_tree_sha: str | None,
    operation: str,
    proposed_paths: list[str],
    resource_classes: list[str] | None = None,
    expected_operation_id: str | None = None,
    expected_workstream_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    required = (
        "schema_version",
        "operation_id",
        "workstream_id",
        "repository",
        "ref",
        "expected_head_sha",
        "operation",
        "allowed_paths",
        "stop_gate",
    )
    failures: list[dict[str, Any]] = []
    for key in required:
        if key not in envelope or envelope[key] in (None, ""):
            failures.append({"code": "AUTHORITY_FIELD_MISSING", "field": key})

    normalized_paths: list[str] = []
    for raw in proposed_paths:
        try:
            normalized_paths.append(_normalize_path(raw))
        except ValueError as exc:
            failures.append({"code": "UNSAFE_PATH", "path": raw, "detail": str(exc)})

    operation_id = str(envelope.get("operation_id") or "")
    workstream_id = str(envelope.get("workstream_id") or "")

    if expected_operation_id and operation_id != expected_operation_id:
        failures.append({
            "code": "OPERATION_ID_MISMATCH",
            "expected": expected_operation_id,
            "actual": operation_id,
        })
    if expected_workstream_id and workstream_id != expected_workstream_id:
        failures.append({
            "code": "WORKSTREAM_ID_MISMATCH",
            "expected": expected_workstream_id,
            "actual": workstream_id,
        })

    if str(envelope.get("repository") or "") != repository:
        failures.append({
            "code": "REPOSITORY_MISMATCH",
            "expected": envelope.get("repository"),
            "actual": repository,
        })
    if str(envelope.get("ref") or "") != ref:
        failures.append({"code": "REF_MISMATCH", "expected": envelope.get("ref"), "actual": ref})
    if str(envelope.get("expected_head_sha") or "") != live_head_sha:
        failures.append({
            "code": "HEAD_MISMATCH",
            "expected": envelope.get("expected_head_sha"),
            "actual": live_head_sha,
        })

    expected_tree = envelope.get("expected_tree_sha")
    if expected_tree and live_tree_sha and str(expected_tree) != live_tree_sha:
        failures.append({
            "code": "TREE_MISMATCH",
            "expected": expected_tree,
            "actual": live_tree_sha,
        })

    if str(envelope.get("operation") or "") != operation:
        failures.append({
            "code": "OPERATION_MISMATCH",
            "expected": envelope.get("operation"),
            "actual": operation,
        })

    expires_at = envelope.get("expires_at")
    if expires_at:
        try:
            expiry = _parse_time(str(expires_at))
            current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            if current >= expiry:
                failures.append({"code": "AUTHORITY_EXPIRED", "expires_at": str(expires_at)})
        except ValueError as exc:
            failures.append({"code": "INVALID_EXPIRY", "detail": str(exc)})

    allowed = [str(item) for item in envelope.get("allowed_paths", [])]
    prohibited = [str(item) for item in envelope.get("prohibited_paths", [])]
    for path in normalized_paths:
        try:
            if any(_matches(pattern, path) for pattern in prohibited):
                failures.append({"code": "PROHIBITED_PATH", "path": path})
                continue
            if not any(_matches(pattern, path) for pattern in allowed):
                failures.append({"code": "PATH_OUTSIDE_WRITE_SET", "path": path})
        except ValueError as exc:
            failures.append({"code": "INVALID_PATH_POLICY", "path": path, "detail": str(exc)})

    requested_resources = set(resource_classes or [])
    authorized_resources = set(str(item) for item in envelope.get("resource_classes", []))
    for resource in sorted(requested_resources - authorized_resources):
        failures.append({"code": "RESOURCE_OUTSIDE_AUTHORITY", "resource": resource})

    write_policy = envelope.get("write_policy") or {}
    max_changed_paths = write_policy.get("max_changed_paths")
    if isinstance(max_changed_paths, int) and len(normalized_paths) > max_changed_paths:
        failures.append({
            "code": "WRITE_SET_TOO_LARGE",
            "max_changed_paths": max_changed_paths,
            "actual": len(normalized_paths),
        })

    return {
        "schema_version": "0.4",
        "valid": not failures,
        "operation_id": operation_id,
        "workstream_id": workstream_id,
        "stop_gate": envelope.get("stop_gate"),
        "expected_head_sha": envelope.get("expected_head_sha"),
        "expected_tree_sha": expected_tree,
        "proposed_paths": normalized_paths,
        "resource_classes": sorted(requested_resources),
        "failures": failures,
    }


def active_lease_conflicts(
    *,
    repository: str,
    ref: str,
    operation_id: str,
    proposed_paths: list[str],
    resource_classes: list[str],
    active_leases: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    normalized_paths = [_normalize_path(path) for path in proposed_paths]
    requested_resources = set(resource_classes)
    conflicts: list[dict[str, Any]] = []

    for lease in active_leases:
        if str(lease.get("state") or "ACTIVE") != "ACTIVE":
            continue
        if lease.get("repository") != repository or lease.get("ref") != ref:
            continue
        if lease.get("operation_id") == operation_id:
            continue

        expires_at = lease.get("expires_at")
        if expires_at:
            try:
                if current >= _parse_time(str(expires_at)):
                    continue
            except ValueError:
                conflicts.append({
                    "code": "INVALID_ACTIVE_LEASE",
                    "lease_id": lease.get("lease_id"),
                })
                continue

        lease_patterns = [str(item) for item in lease.get("path_patterns", [])]
        path_hits = sorted({
            path
            for path in normalized_paths
            if any(_matches(pattern, path) for pattern in lease_patterns)
        })
        resource_hits = sorted(requested_resources & set(lease.get("resource_classes", [])))
        if path_hits or resource_hits:
            conflicts.append({
                "code": "LEASE_CONFLICT",
                "lease_id": lease.get("lease_id"),
                "operation_id": lease.get("operation_id"),
                "path_conflicts": path_hits,
                "resource_conflicts": resource_hits,
            })

    return conflicts


def plan_mutation(
    envelope: dict[str, Any],
    request: dict[str, Any],
    *,
    repository: str,
    ref: str,
    live_head_sha: str,
    live_tree_sha: str | None,
    operation_id: str | None = None,
    workstream_id: str | None = None,
    active_leases: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    operation = str(request.get("operation") or "")
    proposed_paths = [str(item) for item in request.get("proposed_paths", [])]
    resources = [str(item) for item in request.get("resource_classes", [])]
    request_failures: list[dict[str, Any]] = []
    if not operation:
        request_failures.append({"code": "MUTATION_REQUEST_FIELD_MISSING", "field": "operation"})
    if not isinstance(request.get("proposed_paths", []), list):
        request_failures.append({"code": "MUTATION_REQUEST_INVALID", "field": "proposed_paths"})
    if not isinstance(request.get("resource_classes", []), list):
        request_failures.append({"code": "MUTATION_REQUEST_INVALID", "field": "resource_classes"})

    authority = validate_authority(
        envelope,
        repository=repository,
        ref=ref,
        live_head_sha=live_head_sha,
        live_tree_sha=live_tree_sha,
        operation=operation,
        proposed_paths=proposed_paths,
        resource_classes=resources,
        expected_operation_id=operation_id,
        expected_workstream_id=workstream_id,
        now=now,
    )

    lease_conflicts: list[dict[str, Any]] = []
    if authority["valid"] and not request_failures:
        lease_conflicts = active_lease_conflicts(
            repository=repository,
            ref=ref,
            operation_id=authority["operation_id"],
            proposed_paths=authority["proposed_paths"],
            resource_classes=resources,
            active_leases=active_leases or [],
            now=now,
        )

    eligible = authority["valid"] and not request_failures and not lease_conflicts
    return {
        "schema_version": "0.4",
        "mode": "DRY_RUN_ONLY",
        "decision": "AUTHORIZED_DRY_RUN" if eligible else "REJECTED",
        "eligible_for_future_execution": eligible,
        "execution_enabled": False,
        "safe_to_execute_now": False,
        "operation_id": authority.get("operation_id"),
        "workstream_id": authority.get("workstream_id"),
        "request_failures": request_failures,
        "authority": authority,
        "lease_conflicts": lease_conflicts,
        "cas": {
            "expected_head_sha": envelope.get("expected_head_sha"),
            "expected_tree_sha": envelope.get("expected_tree_sha"),
            "live_head_sha": live_head_sha,
            "live_tree_sha": live_tree_sha,
        },
        "lease_request": {
            "operation_id": authority.get("operation_id"),
            "repository": repository,
            "ref": ref,
            "path_patterns": authority.get("proposed_paths", []),
            "resource_classes": resources,
        } if eligible else None,
        "retry_policy": "RECONCILE_LIVE_STATE_BEFORE_ANY_RETRY",
    }


def reconcile_post_write(
    plan: dict[str, Any],
    *,
    live_head_sha: str,
    live_tree_sha: str | None,
    observed_changed_paths: list[str],
    expected_post_sha: str | None = None,
    expected_post_tree_sha: str | None = None,
) -> dict[str, Any]:
    old_head = str(plan.get("cas", {}).get("live_head_sha") or "")
    authorized_paths = [
        str(path) for path in plan.get("authority", {}).get("proposed_paths", [])
    ]
    observed = [_normalize_path(path) for path in observed_changed_paths]
    unexpected = sorted(set(observed) - set(authorized_paths))

    if plan.get("decision") != "AUTHORIZED_DRY_RUN":
        verdict = "PLAN_NOT_AUTHORIZED"
    elif unexpected:
        verdict = "UNAUTHORIZED_POST_WRITE_PATHS"
    elif expected_post_sha and live_head_sha == expected_post_sha:
        if expected_post_tree_sha and live_tree_sha != expected_post_tree_sha:
            verdict = "POST_TREE_MISMATCH"
        else:
            verdict = "WRITE_CONFIRMED_EXPECTED_POST_STATE"
    elif live_head_sha == old_head:
        verdict = "WRITE_NOT_OBSERVED"
    else:
        verdict = "POST_STATE_REQUIRES_RECONCILIATION"

    return {
        "schema_version": "0.4",
        "verdict": verdict,
        "old_head_sha": old_head,
        "live_head_sha": live_head_sha,
        "live_tree_sha": live_tree_sha,
        "expected_post_sha": expected_post_sha,
        "expected_post_tree_sha": expected_post_tree_sha,
        "observed_changed_paths": observed,
        "unexpected_paths": unexpected,
        "safe_to_blind_retry": False,
    }
