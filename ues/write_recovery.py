from __future__ import annotations

from typing import Any

from .idempotency import ACTIVE_STATES, latest_records
from .operation_records import trusted_operation_records


def recover_unobserved_format_operations(
    prior_comments: list[dict[str, Any]],
    *,
    repository: str,
    ref: str,
    live_head_sha: str,
) -> list[dict[str, Any]]:
    """Terminalize stale branch-only format operations only when no remote write is observed.

    This helper is safe only for the current bounded format-fix operation class. If HEAD moved
    away from an active receipt's start SHA, no recovery receipt is emitted and normal branch
    serialization keeps the new operation blocked for explicit reconciliation.
    """
    records = trusted_operation_records(prior_comments)
    recovered: list[dict[str, Any]] = []
    for record in latest_records(records):
        if record.get("repository") != repository or record.get("ref") != ref:
            continue
        if str(record.get("state") or "") not in ACTIVE_STATES:
            continue
        if str(record.get("start_sha") or "") != live_head_sha:
            continue
        replacement = dict(record)
        replacement["schema_version"] = "0.6"
        replacement["state"] = "CANCELLED"
        replacement["safe_to_blind_retry"] = False
        extensions = record.get("extensions") if isinstance(record.get("extensions"), dict) else {}
        replacement["extensions"] = {
            **extensions,
            "recovery": "NO_REMOTE_WRITE_OBSERVED_AT_START_SHA",
            "recovered_live_sha": live_head_sha,
        }
        recovered.append(replacement)
    return recovered
