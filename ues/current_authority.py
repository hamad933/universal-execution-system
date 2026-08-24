from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping


class CurrentAuthorityError(ValueError):
    pass


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CurrentAuthorityError("authority expiry must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise CurrentAuthorityError("authority expiry must include timezone")
    return parsed.astimezone(timezone.utc)


def validate_current_authority(
    adapter: Mapping[str, Any],
    authority: Mapping[str, Any],
    *,
    transport_actor: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a compact current-authority envelope transported by a Controller.

    The envelope is a transport of already reconstructed governed authority; it
    is not a replacement truth owner. It must identify Drive current state,
    exact project/route, a bounded expiry and an allowlisted transport actor.
    """

    project = str(adapter.get("project") or "").strip()
    route = str(adapter.get("route") or project).strip()
    config = adapter.get("authority_transport")
    config = config if isinstance(config, Mapping) else {}
    allowed_actors = {str(item) for item in config.get("controller_actor_allowlist") or [] if str(item)}

    if str(authority.get("source") or "") != "DRIVE_CURRENT_STATE":
        raise CurrentAuthorityError("current authority source must be DRIVE_CURRENT_STATE")
    if str(authority.get("project") or "") != project or str(authority.get("route") or "") != route:
        raise CurrentAuthorityError("authority project/route does not match adapter")
    if authority.get("current") is not True:
        raise CurrentAuthorityError("authority envelope is not marked current")
    event_id = str(authority.get("authority_event_id") or "").strip()
    source_id = str(authority.get("source_id") or "").strip()
    if not event_id or not source_id:
        raise CurrentAuthorityError("authority_event_id and source_id are required")
    actor = str(transport_actor or "").strip()
    if not actor or actor not in allowed_actors:
        raise CurrentAuthorityError("authority transport actor is not allowlisted")

    expires_at = str(authority.get("expires_at") or "").strip()
    if not expires_at:
        raise CurrentAuthorityError("authority envelope requires bounded expires_at")
    expiry = _parse_time(expires_at)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current >= expiry:
        raise CurrentAuthorityError("authority envelope is stale")

    result = dict(authority)
    result["transport"] = {
        "actor": actor,
        "actor_allowlisted": True,
        "canonical_truth_owner": "DRIVE",
        "transport_is_truth_owner": False,
        "expires_at": expiry.isoformat().replace("+00:00", "Z"),
    }
    return result


def load_current_authority_json(
    adapter: Mapping[str, Any],
    raw: str | None,
    *,
    transport_actor: str | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not str(raw or "").strip():
        return None
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise CurrentAuthorityError("current authority JSON is invalid") from exc
    if not isinstance(value, Mapping):
        raise CurrentAuthorityError("current authority JSON must contain an object")
    return validate_current_authority(adapter, value, transport_actor=transport_actor, now=now)


def exact_lineage_authority(
    authority: Mapping[str, Any] | None,
    *,
    workstream: str,
    role: str,
) -> dict[str, Any] | None:
    if not isinstance(authority, Mapping):
        return None
    policy = authority.get("generation_policy")
    policy = policy if isinstance(policy, Mapping) else {}
    lineages = policy.get("authorized_lineages")
    lineages = lineages if isinstance(lineages, Mapping) else {}
    key = f"{workstream}:{str(role).upper()}"
    lane = lineages.get(key)
    if not isinstance(lane, Mapping) or lane.get("authorized") is not True:
        return None
    return dict(lane)


def dynamic_lineages(authority: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(authority, Mapping):
        return {}
    value = authority.get("lineages")
    return dict(value) if isinstance(value, Mapping) else {}
