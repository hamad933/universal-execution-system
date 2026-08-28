from __future__ import annotations

import re
from typing import Any, Mapping

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_INVALID_FOR_REVIEW_EVIDENCE = "INVALID_FOR_REVIEW_EVIDENCE"


def exact_invalid_review_handoff_adjudication(
    *,
    authority_event_id: str,
    lane_authority: Mapping[str, Any] | None,
    project: str,
    route: str,
    workstream: str,
    role: str,
    handoff: Mapping[str, Any] | None,
    binding: Mapping[str, Any],
    state_snapshot: Mapping[str, Any],
) -> bool:
    """Return True only for an exact Parent-adjudicated invalid review handoff.

    The adjudication is data carried by the already validated current-authority
    envelope. It grants no authority by itself. Exact project/route/lineage/role,
    authority event, generation, session fingerprint and both sanitized handoff
    fingerprints must match current runtime evidence. Any missing or stale field
    fails closed.
    """

    if not isinstance(lane_authority, Mapping) or not isinstance(handoff, Mapping):
        return False
    adjudication = lane_authority.get("handoff_adjudication")
    if not isinstance(adjudication, Mapping):
        return False
    if str(adjudication.get("classification") or "").strip().upper() != _INVALID_FOR_REVIEW_EVIDENCE:
        return False

    current_event = str(authority_event_id or "").strip()
    if not current_event or str(adjudication.get("authority_event_id") or "").strip() != current_event:
        return False
    if str(adjudication.get("project") or "").strip() != str(project):
        return False
    if str(adjudication.get("route") or "").strip() != str(route):
        return False
    if str(adjudication.get("workstream") or "").strip() != str(workstream):
        return False
    if str(adjudication.get("role") or "").strip().upper() != str(role).strip().upper():
        return False

    try:
        adjudicated_generation = int(adjudication.get("generation") or 0)
        current_generation = int(state_snapshot.get("generation") or 0)
    except (TypeError, ValueError):
        return False
    if adjudicated_generation < 1 or adjudicated_generation != current_generation:
        return False

    state_fingerprint = str(state_snapshot.get("session_fingerprint") or "").strip()
    binding_fingerprint = str(binding.get("session_fingerprint") or "").strip()
    adjudicated_fingerprint = str(adjudication.get("session_fingerprint") or "").strip()
    if not state_fingerprint or binding_fingerprint != state_fingerprint or adjudicated_fingerprint != state_fingerprint:
        return False

    message_fingerprint = str(handoff.get("message_fingerprint") or "").strip().lower()
    activity_fingerprint = str(handoff.get("activity_fingerprint") or "").strip().lower()
    adjudicated_message = str(adjudication.get("handoff_message_fingerprint") or "").strip().lower()
    adjudicated_activity = str(adjudication.get("handoff_activity_fingerprint") or "").strip().lower()
    if not _HEX64.fullmatch(message_fingerprint) or not _HEX64.fullmatch(activity_fingerprint):
        return False
    if adjudicated_message != message_fingerprint or adjudicated_activity != activity_fingerprint:
        return False

    return True
