from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    items = [item.strip() for item in value.split(",")]
    if any(not item for item in items):
        raise ValueError("comma-separated values must not contain empty items")
    return items


def canonical_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def derive_owner_comment_authority(
    arguments: dict[str, str],
    *,
    actor: str,
    repository_owner: str,
    repository: str,
    pr_number: int,
    comment_id: str,
    comment_created_at: str,
    candidate_ref: str,
    candidate_head_sha: str,
    candidate_tree_sha: str | None,
    workstream_id: str,
    ttl_seconds: int = 900,
) -> dict[str, Any]:
    if actor != repository_owner:
        raise ValueError("authority event actor is not repository owner")
    if not comment_id:
        raise ValueError("authority event requires comment_id")
    if not SHA_RE.fullmatch(candidate_head_sha):
        raise ValueError("candidate head SHA is invalid")

    allowed = {"operation", "sha", "ref", "paths", "resources", "max_paths"}
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ValueError(f"unsupported authority arguments: {', '.join(unknown)}")

    operation = arguments.get("operation")
    expected_sha = arguments.get("sha")
    expected_ref = arguments.get("ref")
    if not operation:
        raise ValueError("mutation-authorize requires operation=<operation>")
    if not expected_sha:
        raise ValueError("mutation-authorize requires sha=<exact candidate SHA>")
    if not expected_ref:
        raise ValueError("mutation-authorize requires ref=<exact candidate ref>")
    if expected_sha != candidate_head_sha:
        raise ValueError("owner authority SHA does not match live candidate HEAD")
    if expected_ref != candidate_ref:
        raise ValueError("owner authority ref does not match live candidate ref")

    paths = _csv(arguments.get("paths"))
    if not paths:
        raise ValueError("mutation-authorize requires at least one path")
    resources = _csv(arguments.get("resources"))

    max_paths_raw = arguments.get("max_paths")
    max_paths = len(paths)
    if max_paths_raw is not None:
        try:
            max_paths = int(max_paths_raw)
        except ValueError as exc:
            raise ValueError("max_paths must be an integer") from exc
        if max_paths < 1:
            raise ValueError("max_paths must be positive")

    issued_at = _parse_time(comment_created_at)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    operation_id = f"github-comment:{comment_id}"
    authority_event = {
        "source": "github_issue_comment",
        "event_id": str(comment_id),
        "actor": actor,
        "repository_owner": repository_owner,
        "repository": repository,
        "pr_number": pr_number,
        "candidate_ref": candidate_ref,
        "candidate_head_sha": candidate_head_sha,
        "candidate_tree_sha": candidate_tree_sha,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    request = {
        "schema_version": "0.5",
        "operation": operation,
        "proposed_paths": paths,
        "resource_classes": resources,
    }
    envelope = {
        "schema_version": "0.5",
        "operation_id": operation_id,
        "workstream_id": workstream_id,
        "actor": actor,
        "repository": repository,
        "ref": candidate_ref,
        "expected_head_sha": candidate_head_sha,
        "expected_tree_sha": candidate_tree_sha,
        "operation": operation,
        "allowed_paths": paths,
        "prohibited_paths": [],
        "resource_classes": resources,
        "expires_at": expires_at.isoformat(),
        "stop_gate": "HEAD_OR_TREE_MOVED",
        "write_policy": {"max_changed_paths": max_paths},
        "extensions": {
            "authority_source": "trusted_owner_comment",
            "authority_event_id": str(comment_id),
            "authority_event_digest": canonical_digest(authority_event),
        },
    }
    return {
        "schema_version": "0.5",
        "trusted": True,
        "authority_event": authority_event,
        "authority_envelope": envelope,
        "mutation_request": request,
        "operation_id": operation_id,
        "request_digest": canonical_digest(request),
    }
