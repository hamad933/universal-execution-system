from __future__ import annotations

from urllib.parse import quote, unquote

LaneKey = tuple[str, str, str]
_PREFIX = "ues-lane:v1"


def _component(value: object, name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"{name} is required for canonical lane identity")
    return text


def canonical_lane_id(project: object, route: object, workstream: object) -> str:
    """Return the single reversible scalar key for a complete portfolio lane tuple."""

    components = (
        _component(project, "project"),
        _component(route, "route"),
        _component(workstream, "workstream"),
    )
    encoded = [quote(item, safe="") for item in components]
    return _PREFIX + "|" + "|".join(encoded)


def lane_id_from_key(key: LaneKey) -> str:
    if not isinstance(key, tuple) or len(key) != 3:
        raise ValueError("canonical lane key must be a (project, route, workstream) tuple")
    return canonical_lane_id(*key)


def parse_lane_id(lane_id: object) -> LaneKey:
    value = str(lane_id) if lane_id is not None else ""
    parts = value.split("|")
    if len(parts) != 4 or parts[0] != _PREFIX:
        raise ValueError("invalid canonical lane_id")

    decoded = tuple(unquote(part) for part in parts[1:])
    if any(not item.strip() for item in decoded):
        raise ValueError("canonical lane_id contains an empty identity component")

    result: LaneKey = (decoded[0], decoded[1], decoded[2])
    if canonical_lane_id(*result) != value:
        raise ValueError("lane_id is not in canonical encoding")
    return result
