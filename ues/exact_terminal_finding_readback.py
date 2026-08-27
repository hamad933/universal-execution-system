from __future__ import annotations

import argparse
import json
import re
from typing import Any, Mapping

from . import terminal_lifecycle

_ALLOWED_PROJECTS = frozenset({"RP01", "RP02", "RP03", "RP04"})
_WORKSTREAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FINDING_KEYS = (
    "finding_id",
    "severity",
    "path",
    "resource",
    "locator",
    "summary",
    "recommended_action",
    "evidence_references",
)


def _safe_scalar(value: Any, *, limit: int = 4096) -> str | int | float | bool | None:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:limit]


def _safe_evidence_references(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:2048] for item in value[:32] if item is not None]


def _project_finding(raw: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key in _FINDING_KEYS:
        if key not in raw:
            continue
        if key == "evidence_references":
            projected[key] = _safe_evidence_references(raw.get(key))
        else:
            projected[key] = _safe_scalar(raw.get(key))
    return projected


def _project_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    findings = raw.get("findings")
    findings = findings if isinstance(findings, list) else []
    return {
        key: raw.get(key)
        for key in (
            "project",
            "route",
            "logical_workstream",
            "role",
            "generation",
            "session_fingerprint",
            "repository",
            "status",
            "verdict",
            "candidate_sha",
            "reviewed_sha",
            "finding_count",
            "result_state",
            "freshness_status",
            "result_fingerprint",
            "current_view_fingerprint",
            "parent_action_required",
            "safe_read_only_recovery_exists",
        )
        if key in raw
    } | {
        "findings": [_project_finding(item) for item in findings if isinstance(item, Mapping)],
    }


def run(project: str, workstream: str) -> dict[str, Any]:
    project_id = str(project or "").strip().upper()
    target = str(workstream or "").strip()
    if project_id not in _ALLOWED_PROJECTS:
        raise ValueError("exact terminal finding readback project must be RP01-RP04")
    if not _WORKSTREAM.fullmatch(target):
        raise ValueError("exact terminal finding readback workstream is invalid")

    lifecycle = terminal_lifecycle.run(project_id)
    matches = [
        item
        for item in lifecycle.get("results") or []
        if isinstance(item, Mapping) and str(item.get("logical_workstream") or "") == target
    ]
    return {
        "schema_version": "UES_EXACT_TERMINAL_FINDING_READBACK_V1",
        "project": project_id,
        "workstream": target,
        "match_count": len(matches),
        "results": [_project_result(item) for item in matches],
        "provider_live_read_performed": False,
        "provider_mutation_performed": False,
        "external_effects_dispatched": 0,
        "new_tasks_or_sessions_created": 0,
        "raw_activity_content_persisted": False,
        "raw_session_ids_persisted": False,
        "private_source_identity_persisted": False,
        "safe_to_blind_retry": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UES sanitized exact terminal finding readback")
    parser.add_argument("project", choices=sorted(_ALLOWED_PROJECTS))
    parser.add_argument("workstream")
    args = parser.parse_args(argv)
    result = run(args.project, args.workstream)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("match_count") == 1 else 2


if __name__ == "__main__":
    raise SystemExit(main())
