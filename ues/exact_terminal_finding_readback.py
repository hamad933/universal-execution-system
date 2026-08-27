from __future__ import annotations

import argparse
import json
import re
from typing import Any, Mapping

from . import terminal_recovery as recovery
from .identity import canonical_lane_id
from .live_runtime import build_live_state_store
from .terminal_results import logical_lineage_key

_ALLOWED_PROJECTS = frozenset({"RP01", "RP02", "RP03", "RP04"})
_ALLOWED_ROLES = ("REVIEWER", "ASSURANCE", "WRITER")
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
            "parent_action_required",
            "safe_read_only_recovery_exists",
        )
        if key in raw
    } | {
        "findings": [_project_finding(item) for item in findings if isinstance(item, Mapping)],
    }


def _exact_persisted_result(store: Any, *, project: str, route: str, workstream: str) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for role in _ALLOWED_ROLES:
        lane_workstream = logical_lineage_key(workstream, role)
        lane_id = canonical_lane_id(project, route, lane_workstream)
        read = store.read_workstream(lane_id)
        if read.status != "OK" or read.record is None:
            continue
        record = read.record
        if str(record.project or "") != project or str(record.route or "") != route:
            continue
        evidence = record.evidence_bindings or {}
        if str(evidence.get("workstream") or "") != workstream:
            continue
        if str(evidence.get("role") or "").upper() != role:
            continue
        stored = evidence.get(recovery.TERMINAL_RESULT_KEY)
        if not isinstance(stored, Mapping):
            continue

        exact = (
            str(stored.get("logical_workstream") or "") == workstream
            and str(stored.get("role") or "").upper() == role
            and int(stored.get("generation") or 0) == int(evidence.get("generation") or 0)
            and str(stored.get("session_fingerprint") or "").lower()
            == str(evidence.get("session_fingerprint") or "").lower()
        )
        if not exact:
            continue

        current_sha = str(evidence.get("current_candidate_sha") or "").lower()
        if role in {"REVIEWER", "ASSURANCE"} and current_sha:
            if str(stored.get("reviewed_sha") or "").lower() != current_sha:
                continue
        elif role == "WRITER" and current_sha:
            candidate = str(stored.get("candidate_sha") or "").lower()
            if candidate and candidate != current_sha:
                continue
        matches.append(dict(stored))

    return matches[0] if len(matches) == 1 else None


def run(project: str, workstream: str, *, store: Any | None = None) -> dict[str, Any]:
    project_id = str(project or "").strip().upper()
    target = str(workstream or "").strip()
    if project_id not in _ALLOWED_PROJECTS:
        raise ValueError("exact terminal finding readback project must be RP01-RP04")
    if not _WORKSTREAM.fullmatch(target):
        raise ValueError("exact terminal finding readback workstream is invalid")

    projects = recovery.load_governed_projects([project_id])
    if len(projects) != 1:
        raise ValueError("exact terminal finding readback requires one governed project adapter")
    route = str(projects[0]["route"])
    live_store = store or build_live_state_store()
    result = _exact_persisted_result(live_store, project=project_id, route=route, workstream=target)
    matches = [result] if isinstance(result, Mapping) else []
    return {
        "schema_version": "UES_EXACT_TERMINAL_FINDING_READBACK_V3",
        "project": project_id,
        "workstream": target,
        "match_count": len(matches),
        "results": [_project_result(item) for item in matches],
        "durable_lane_direct_read": True,
        "canonical_lane_identity_used": True,
        "bounded_role_lane_reads": len(_ALLOWED_ROLES),
        "lane_discovery_performed": False,
        "project_wide_lifecycle_scan_performed": False,
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
