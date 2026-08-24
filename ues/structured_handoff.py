from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0"
START_MARKER = "<UES_HANDOFF_V1>"
END_MARKER = "</UES_HANDOFF_V1>"
FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
VALID_ROLES = {"WRITER", "REVIEWER", "ASSURANCE"}
VALID_STATUSES = {"COMPLETE", "BLOCKED", "NEEDS_INPUT", "CONTEXT_EXHAUSTED", "IN_PROGRESS"}
VALID_VERDICTS = {"PASS", "FINDINGS", "FAIL", "NOT_APPLICABLE", "UNKNOWN"}


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(raw.encode("utf-8")).hexdigest()


def _message_from_activity(activity: Mapping[str, Any]) -> str | None:
    payload = activity.get("agentMessaged")
    if not isinstance(payload, Mapping):
        return None
    message = payload.get("agentMessage")
    return str(message) if isinstance(message, str) and message else None


def _extract_payload(message: str) -> Mapping[str, Any] | None:
    start = message.rfind(START_MARKER)
    if start < 0:
        return None
    end = message.find(END_MARKER, start + len(START_MARKER))
    if end < 0:
        return None
    raw = message[start + len(START_MARKER) : end].strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, Mapping) else None


def normalize_handoff(
    value: Mapping[str, Any],
    *,
    expected_workstream: str | None = None,
    expected_role: str | None = None,
) -> dict[str, Any]:
    role = str(value.get("role") or "").strip().upper()
    workstream = str(value.get("workstream") or "").strip()
    status = str(value.get("status") or "").strip().upper()
    verdict = str(value.get("verdict") or "UNKNOWN").strip().upper()
    candidate_sha = str(value.get("candidate_sha") or "").strip() or None
    reviewed_sha = str(value.get("reviewed_sha") or "").strip() or None
    context_state = str(value.get("context_state") or "OK").strip().upper()
    findings = value.get("findings") or []

    if role not in VALID_ROLES:
        raise ValueError("structured handoff role is invalid")
    if not workstream:
        raise ValueError("structured handoff workstream is required")
    if status not in VALID_STATUSES:
        raise ValueError("structured handoff status is invalid")
    if verdict not in VALID_VERDICTS:
        raise ValueError("structured handoff verdict is invalid")
    if candidate_sha and not FULL_SHA.fullmatch(candidate_sha):
        raise ValueError("candidate_sha must be a full SHA")
    if reviewed_sha and not FULL_SHA.fullmatch(reviewed_sha):
        raise ValueError("reviewed_sha must be a full SHA")
    if not isinstance(findings, list) or not all(isinstance(item, Mapping) for item in findings):
        raise ValueError("structured handoff findings must be a list of objects")
    if expected_workstream and workstream != expected_workstream:
        raise ValueError("structured handoff workstream does not match lineage")
    if expected_role and role != expected_role.upper():
        raise ValueError("structured handoff role does not match lineage")

    safe_findings = [
        {
            "id": str(item.get("id") or "").strip() or f"finding-{index + 1}",
            "severity": str(item.get("severity") or "UNKNOWN").strip().upper(),
            "path": str(item.get("path") or "").strip() or None,
        }
        for index, item in enumerate(findings)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "role": role,
        "workstream": workstream,
        "status": status,
        "verdict": verdict,
        "candidate_sha": candidate_sha,
        "reviewed_sha": reviewed_sha,
        "context_state": context_state,
        "finding_count": len(findings),
        "finding_metadata": safe_findings,
        "finding_payload_fingerprint": _fingerprint(findings) if findings else None,
        "raw_finding_content_persisted": False,
        "raw_message_persisted": False,
    }


def find_latest_structured_handoff(
    activities: Sequence[Mapping[str, Any]],
    *,
    expected_workstream: str | None = None,
    expected_role: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest valid structured agent handoff without returning raw prose."""

    for activity in reversed(list(activities)):
        message = _message_from_activity(activity)
        if not message:
            continue
        payload = _extract_payload(message)
        if payload is None:
            continue
        try:
            normalized = normalize_handoff(
                payload,
                expected_workstream=expected_workstream,
                expected_role=expected_role,
            )
        except ValueError:
            continue
        normalized["message_fingerprint"] = sha256(message.encode("utf-8")).hexdigest()
        normalized["activity_fingerprint"] = sha256(
            str(activity.get("name") or activity.get("id") or "").encode("utf-8")
        ).hexdigest()
        return normalized
    return None


def build_required_handoff_instructions(role: str, workstream: str) -> str:
    role = role.strip().upper()
    if role not in VALID_ROLES:
        raise ValueError("unsupported handoff role")
    return (
        "At every material stop, finish your response with exactly one machine-readable handoff block. "
        "Do not omit it even when blocked or context is exhausted. Use this shape:\n"
        f"{START_MARKER}\n"
        "{\n"
        f'  "role": "{role}",\n'
        f'  "workstream": "{workstream}",\n'
        '  "status": "COMPLETE|BLOCKED|NEEDS_INPUT|CONTEXT_EXHAUSTED|IN_PROGRESS",\n'
        '  "verdict": "PASS|FINDINGS|FAIL|NOT_APPLICABLE|UNKNOWN",\n'
        '  "candidate_sha": null,\n'
        '  "reviewed_sha": null,\n'
        '  "context_state": "OK|EXHAUSTED",\n'
        '  "findings": []\n'
        "}\n"
        f"{END_MARKER}"
    )
