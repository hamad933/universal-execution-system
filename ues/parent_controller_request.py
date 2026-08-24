from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "UES_PARENT_CONTROLLER_REQUEST_V1"
SUPPORTED_PROJECTS = frozenset({"GS", "CEP", "RP01", "RP02", "RP03", "RP04"})
EXPECTED_ROUTES = {
    "GS": "GS",
    "CEP": "PERSONAL:CEP",
    "RP01": "RP01",
    "RP02": "RP02",
    "RP03": "RP03",
    "RP04": "RP04",
}
SUPPORTED_WAKEUP_TYPES = frozenset({"EXTERNAL_RECONCILIATION_REQUEST"})
MAX_REQUEST_BYTES = 128 * 1024
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_ALLOWED_TOP_LEVEL = frozenset(
    {"schema_version", "request_id", "project", "runtime_sha", "current_authority", "wakeup"}
)
_ALLOWED_WAKEUP = frozenset({"event_type", "event_id", "repository", "workstream", "sha"})
_FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "client_secret",
        "private_key",
    }
)


class ParentControllerRequestError(ValueError):
    pass


def _reject_json_constant(value: str) -> None:
    raise ParentControllerRequestError(f"non-standard JSON constant is not allowed: {value}")


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ParentControllerRequestError(f"{field} must be a non-empty string")
    return value.strip()


def _contains_secret_key(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_SECRET_KEYS:
                return str(key)
            found = _contains_secret_key(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _contains_secret_key(nested)
            if found:
                return found
    return None


def validate_parent_controller_request(
    request: Mapping[str, Any],
    *,
    expected_runtime_sha: str | None = None,
) -> dict[str, Any]:
    """Validate a low-friction Parent Controller request transported by `ues-control`.

    The request file is transport/routing data only. It cannot grant project
    authority. `current_authority` is passed unchanged to the existing UES
    current-authority validators and runtime gates after this transport-level
    validation succeeds.
    """

    unknown = sorted(str(key) for key in request if key not in _ALLOWED_TOP_LEVEL)
    if unknown:
        raise ParentControllerRequestError(
            "request contains unsupported fields: " + ", ".join(unknown)
        )

    if request.get("schema_version") != SCHEMA_VERSION:
        raise ParentControllerRequestError(f"schema_version must be {SCHEMA_VERSION}")

    request_id = _required_text(request.get("request_id"), field="request_id")
    if not _REQUEST_ID.fullmatch(request_id):
        raise ParentControllerRequestError("request_id has invalid format")

    project = _required_text(request.get("project"), field="project").upper()
    if project not in SUPPORTED_PROJECTS:
        raise ParentControllerRequestError("project is not supported by Parent Controller ingress")

    runtime_sha = _required_text(request.get("runtime_sha"), field="runtime_sha").lower()
    if not _SHA.fullmatch(runtime_sha):
        raise ParentControllerRequestError("runtime_sha must be a full 40-hex commit SHA")
    if expected_runtime_sha is not None:
        expected = _required_text(expected_runtime_sha, field="expected_runtime_sha").lower()
        if not _SHA.fullmatch(expected):
            raise ParentControllerRequestError("expected_runtime_sha must be a full 40-hex commit SHA")
        if runtime_sha != expected:
            raise ParentControllerRequestError("request runtime_sha does not match live UES main")

    authority = request.get("current_authority")
    if not isinstance(authority, Mapping):
        raise ParentControllerRequestError("current_authority must be an object")
    if authority.get("source") != "DRIVE_CURRENT_STATE":
        raise ParentControllerRequestError("current_authority.source must be DRIVE_CURRENT_STATE")
    if authority.get("current") is not True:
        raise ParentControllerRequestError("current_authority.current must be true")
    if _required_text(authority.get("project"), field="current_authority.project").upper() != project:
        raise ParentControllerRequestError("current_authority.project does not match request project")
    expected_route = EXPECTED_ROUTES[project]
    if _required_text(authority.get("route"), field="current_authority.route") != expected_route:
        raise ParentControllerRequestError("current_authority.route does not match governed project route")
    _required_text(authority.get("authority_event_id"), field="current_authority.authority_event_id")
    _required_text(authority.get("source_id"), field="current_authority.source_id")
    _required_text(authority.get("expires_at"), field="current_authority.expires_at")

    secret_key = _contains_secret_key(request)
    if secret_key:
        raise ParentControllerRequestError(
            f"request payload must not contain secret-bearing key: {secret_key}"
        )

    wakeup_raw = request.get("wakeup")
    if wakeup_raw is None:
        wakeup_raw = {}
    if not isinstance(wakeup_raw, Mapping):
        raise ParentControllerRequestError("wakeup must be an object")
    unknown_wakeup = sorted(str(key) for key in wakeup_raw if key not in _ALLOWED_WAKEUP)
    if unknown_wakeup:
        raise ParentControllerRequestError(
            "wakeup contains unsupported fields: " + ", ".join(unknown_wakeup)
        )

    event_type = str(wakeup_raw.get("event_type") or "EXTERNAL_RECONCILIATION_REQUEST").strip()
    if event_type not in SUPPORTED_WAKEUP_TYPES:
        raise ParentControllerRequestError("wakeup.event_type is not allowlisted")
    event_id = str(wakeup_raw.get("event_id") or request_id).strip()
    if not _REQUEST_ID.fullmatch(event_id):
        raise ParentControllerRequestError("wakeup.event_id has invalid format")

    repository = str(wakeup_raw.get("repository") or "").strip()
    workstream = str(wakeup_raw.get("workstream") or "").strip()
    wakeup_sha = str(wakeup_raw.get("sha") or "").strip().lower()
    if wakeup_sha and not _SHA.fullmatch(wakeup_sha):
        raise ParentControllerRequestError("wakeup.sha must be empty or a full 40-hex commit SHA")

    result = dict(request)
    result["project"] = project
    result["runtime_sha"] = runtime_sha
    result["current_authority"] = dict(authority)
    result["wakeup"] = {
        "event_type": event_type,
        "event_id": event_id,
        "repository": repository,
        "workstream": workstream,
        "sha": wakeup_sha,
    }
    return result


def load_parent_controller_request(
    raw: str,
    *,
    expected_runtime_sha: str | None = None,
) -> dict[str, Any]:
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ParentControllerRequestError("Parent Controller request exceeds maximum size")
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise ParentControllerRequestError("Parent Controller request is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise ParentControllerRequestError("Parent Controller request must contain an object")
    return validate_parent_controller_request(value, expected_runtime_sha=expected_runtime_sha)


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate UES Parent Controller control-queue request")
    parser.add_argument("--input", required=True)
    parser.add_argument("--expected-runtime-sha", required=True)
    parser.add_argument("--authority-output", required=True)
    parser.add_argument("--metadata-output", required=True)
    args = parser.parse_args(argv)

    raw = Path(args.input).read_text(encoding="utf-8")
    request = load_parent_controller_request(raw, expected_runtime_sha=args.expected_runtime_sha)
    authority = request["current_authority"]
    wakeup = request["wakeup"]

    Path(args.authority_output).write_text(
        json.dumps(authority, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request["request_id"],
        "request_digest": _canonical_digest(request),
        "authority_digest": _canonical_digest(authority),
        "authority_event_id": authority["authority_event_id"],
        "project": request["project"],
        "runtime_sha": request["runtime_sha"],
        "wakeup": wakeup,
        "request_file_is_truth_owner": False,
        "canonical_authority_source": "DRIVE_CURRENT_STATE",
        "secrets_allowed_in_request": False,
        "safe_to_blind_retry": False,
    }
    Path(args.metadata_output).write_text(
        json.dumps(metadata, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metadata, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
