from __future__ import annotations

import base64
import json
import re
from typing import Any, Iterable

MARKER_RE = re.compile(r"<!-- UES_OPERATION_RECEIPT:([A-Za-z0-9_-]+) -->")
TRUSTED_RECEIPT_AUTHORS = {"github-actions[bot]"}


def _encode_payload(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_payload(token: str) -> dict[str, Any]:
    padding = "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode((token + padding).encode("ascii"))
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("operation receipt marker payload must be an object")
    return value


def render_receipt_comment(receipt: dict[str, Any]) -> str:
    token = _encode_payload(receipt)
    state = str(receipt.get("state") or "UNKNOWN")
    operation_id = str(receipt.get("operation_id") or "unknown")
    start_sha = str(receipt.get("start_sha") or "unknown")
    final_sha = receipt.get("final_sha")
    extensions = receipt.get("extensions") if isinstance(receipt.get("extensions"), dict) else {}
    operation = str(extensions.get("operation") or "unknown")
    lines = [
        f"<!-- UES_OPERATION_RECEIPT:{token} -->",
        "### UES operation receipt",
        f"- operation: `{operation}`",
        f"- operation ID: `{operation_id}`",
        f"- state: **{state}**",
        f"- start SHA: `{start_sha}`",
    ]
    if final_sha:
        lines.append(f"- final SHA: `{final_sha}`")
    lines.append("- blind retry: **never**; reconcile live state first")
    return "\n".join(lines)


def parse_receipt_markers(body: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for match in MARKER_RE.finditer(body or ""):
        records.append(_decode_payload(match.group(1)))
    return records


def trusted_operation_records(
    comments: Iterable[dict[str, Any]],
    *,
    trusted_authors: set[str] | None = None,
) -> list[dict[str, Any]]:
    authors = trusted_authors or TRUSTED_RECEIPT_AUTHORS
    records: list[dict[str, Any]] = []
    for comment in comments:
        author = str(comment.get("author") or comment.get("user") or "")
        if author not in authors:
            continue
        body = str(comment.get("body") or "")
        try:
            records.extend(parse_receipt_markers(body))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            continue
    return records
