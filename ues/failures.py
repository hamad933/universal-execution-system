from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping

FAILURE_CATEGORIES = {
    "CANDIDATE_CODE_DEFECT",
    "CANDIDATE_TEST_DEFECT",
    "CANDIDATE_FORMAT_DEFECT",
    "CANDIDATE_BUILD_DEFECT",
    "SHARED_BASELINE_DEFECT",
    "DEPENDENCY_SECURITY_DEFECT",
    "INFRASTRUCTURE_TRANSIENT",
    "INFRASTRUCTURE_PERSISTENT",
    "CI_CONFIGURATION_DEFECT",
    "AUTHORIZATION_OR_BILLING_BLOCKER",
    "UPSTREAM_SERVICE_FAILURE",
    "FLAKY_TEST_SUSPECTED",
    "STALE_OR_WRONG_CANDIDATE",
    "NOT_APPLICABLE",
    "UNKNOWN_REQUIRES_TRIAGE",
    "PROVIDER_AUTHENTICATION",
    "PROVIDER_AUTHORIZATION",
    "PROVIDER_NOT_FOUND",
    "PROVIDER_RATE_LIMIT",
    "PROVIDER_SERVER_ERROR",
    "PROVIDER_NETWORK_ERROR",
    "PROVIDER_PROTOCOL_ERROR",
    "WRITE_OUTCOME_UNKNOWN",
}

CANDIDATE_STAGE_CATEGORY = {
    "code": "CANDIDATE_CODE_DEFECT",
    "lint": "CANDIDATE_CODE_DEFECT",
    "typecheck": "CANDIDATE_CODE_DEFECT",
    "test": "CANDIDATE_TEST_DEFECT",
    "format": "CANDIDATE_FORMAT_DEFECT",
    "build": "CANDIDATE_BUILD_DEFECT",
}

_EXPLICIT_ROOT_FIELDS = (
    "root_evidence_id",
    "shared_root_id",
    "incident_id",
)


def classify_failure(failure: dict[str, Any]) -> dict[str, Any]:
    origin = str(failure.get("origin") or "unknown").lower()
    stage = str(failure.get("stage") or "unknown").lower()
    base_reproduces = failure.get("base_reproduces")
    retry_count = int(failure.get("retry_count") or 0)
    transient = failure.get("transient")
    stale_candidate = bool(failure.get("stale_candidate"))
    flaky = bool(failure.get("flaky"))
    security = bool(failure.get("security"))
    billing = bool(failure.get("billing"))
    upstream = bool(failure.get("upstream"))

    reasons: list[str] = []
    confidence = "MEDIUM"

    if stale_candidate:
        category = "STALE_OR_WRONG_CANDIDATE"
        reasons.append("failure evidence is not bound to the intended candidate")
        confidence = "HIGH"
    elif billing or origin in {"authorization", "billing"}:
        category = "AUTHORIZATION_OR_BILLING_BLOCKER"
        reasons.append("execution is blocked by authorization or billing state")
        confidence = "HIGH"
    elif upstream or origin == "upstream":
        category = "UPSTREAM_SERVICE_FAILURE"
        reasons.append("failure originates from an upstream service")
    elif origin == "infrastructure":
        if transient is True:
            category = "INFRASTRUCTURE_TRANSIENT"
            reasons.append("infrastructure failure is explicitly marked transient")
            confidence = "HIGH"
        elif transient is False and retry_count >= 1:
            category = "INFRASTRUCTURE_PERSISTENT"
            reasons.append("infrastructure failure persisted after retry")
        else:
            category = "UNKNOWN_REQUIRES_TRIAGE"
            reasons.append("infrastructure persistence is not established")
            confidence = "LOW"
    elif origin == "ci":
        category = "CI_CONFIGURATION_DEFECT"
        reasons.append("failure is attributed to CI configuration")
        confidence = "HIGH"
    elif security or origin == "dependency-security":
        category = "DEPENDENCY_SECURITY_DEFECT"
        reasons.append("failure is dependency/security scoped")
        confidence = "HIGH"
    elif base_reproduces is True or origin == "baseline":
        category = "SHARED_BASELINE_DEFECT"
        reasons.append("same failure reproduces on the baseline/shared state")
        confidence = "HIGH"
    elif flaky:
        category = "FLAKY_TEST_SUSPECTED"
        reasons.append("failure is explicitly flagged as non-deterministic")
    elif origin == "candidate" and stage in CANDIDATE_STAGE_CATEGORY:
        category = CANDIDATE_STAGE_CATEGORY[stage]
        reasons.append(f"failure is candidate-attributed at {stage} stage")
        confidence = "HIGH"
    elif origin == "not-applicable":
        category = "NOT_APPLICABLE"
        reasons.append("check is explicitly not applicable")
        confidence = "HIGH"
    else:
        category = "UNKNOWN_REQUIRES_TRIAGE"
        reasons.append("available structured signals do not establish a safe category")
        confidence = "LOW"

    return {
        "schema_version": "0.3",
        "category": category,
        "confidence": confidence,
        "reasons": reasons,
        "input": failure,
    }


def classify_provider_failure(failure: dict[str, Any]) -> dict[str, Any]:
    """Classify provider failures without treating ambiguous mutations as retryable."""

    status = failure.get("status_code")
    network = bool(failure.get("network_error"))
    protocol = bool(failure.get("protocol_error"))
    ambiguous_write = bool(failure.get("write_outcome_unknown"))
    retry_after = failure.get("retry_after")

    if ambiguous_write:
        category = "WRITE_OUTCOME_UNKNOWN"
        retry_class = "AUTHORITATIVE_POST_READ_REQUIRED"
    elif status == 401:
        category = "PROVIDER_AUTHENTICATION"
        retry_class = "NO_RETRY_WITHOUT_CREDENTIAL_CHANGE"
    elif status == 403:
        category = "PROVIDER_AUTHORIZATION"
        retry_class = "NO_RETRY_WITHOUT_AUTHORITY_CHANGE"
    elif status == 404:
        category = "PROVIDER_NOT_FOUND"
        retry_class = "NO_RETRY_WITHOUT_BINDING_RECONCILIATION"
    elif status == 429:
        category = "PROVIDER_RATE_LIMIT"
        retry_class = "BOUNDED_READ_RETRY_ONLY"
    elif isinstance(status, int) and 500 <= status <= 599:
        category = "PROVIDER_SERVER_ERROR"
        retry_class = "BOUNDED_READ_RETRY_ONLY"
    elif network:
        category = "PROVIDER_NETWORK_ERROR"
        retry_class = "BOUNDED_READ_RETRY_ONLY"
    elif protocol:
        category = "PROVIDER_PROTOCOL_ERROR"
        retry_class = "FAIL_CLOSED"
    else:
        category = "UNKNOWN_REQUIRES_TRIAGE"
        retry_class = "FAIL_CLOSED"

    return {
        "schema_version": "0.4",
        "category": category,
        "confidence": "HIGH" if category != "UNKNOWN_REQUIRES_TRIAGE" else "LOW",
        "retry_class": retry_class,
        "retry_after": retry_after if category == "PROVIDER_RATE_LIMIT" else None,
        "safe_to_blind_retry": False,
    }


def scope_blocker(classification: dict[str, Any], workstream_id: str) -> dict[str, Any]:
    category = classification["category"]

    if category.startswith("CANDIDATE_"):
        scope = "WORKSTREAM"
        blocks = [workstream_id]
        remediation_owner = "CURRENT_WORKSTREAM"
    elif category in {"SHARED_BASELINE_DEFECT", "DEPENDENCY_SECURITY_DEFECT", "CI_CONFIGURATION_DEFECT"}:
        scope = "SHARED_DOMAIN"
        blocks = []
        remediation_owner = "SEPARATE_SHARED_LANE"
    elif category in {
        "INFRASTRUCTURE_TRANSIENT",
        "INFRASTRUCTURE_PERSISTENT",
        "AUTHORIZATION_OR_BILLING_BLOCKER",
        "UPSTREAM_SERVICE_FAILURE",
        "PROVIDER_AUTHENTICATION",
        "PROVIDER_AUTHORIZATION",
        "PROVIDER_RATE_LIMIT",
        "PROVIDER_SERVER_ERROR",
        "PROVIDER_NETWORK_ERROR",
    }:
        scope = "EXTERNAL"
        blocks = []
        remediation_owner = "PLATFORM_OR_EXTERNAL_OWNER"
    elif category in {"PROVIDER_NOT_FOUND", "PROVIDER_PROTOCOL_ERROR", "WRITE_OUTCOME_UNKNOWN"}:
        scope = "EVIDENCE"
        blocks = [workstream_id]
        remediation_owner = "CONTROL_PLANE"
    elif category == "STALE_OR_WRONG_CANDIDATE":
        scope = "EVIDENCE"
        blocks = [workstream_id]
        remediation_owner = "CONTROL_PLANE"
    elif category == "NOT_APPLICABLE":
        scope = "NONE"
        blocks = []
        remediation_owner = "NONE"
    else:
        scope = "UNKNOWN"
        blocks = [workstream_id]
        remediation_owner = "READ_ONLY_TRIAGE"

    return {
        "schema_version": "0.3",
        "category": category,
        "scope": scope,
        "blocks": blocks,
        "does_not_implicitly_block_unrelated_workstreams": True,
        "remediation_owner": remediation_owner,
        "automatic_write_authorized": False,
    }


def _root_identity(failure: Mapping[str, Any]) -> str | None:
    """Return only an explicit structured root/evidence identity.

    Similar text, exception messages, stages, jobs, or categories are deliberately
    insufficient to collapse failures. The producer must provide one of the frozen
    explicit identity fields.
    """

    for field in _EXPLICIT_ROOT_FIELDS:
        value = failure.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _lane_identity(failure: Mapping[str, Any]) -> str | None:
    lane_id = failure.get("lane_id")
    if isinstance(lane_id, str) and lane_id.strip():
        return lane_id.strip()

    project = failure.get("project")
    route = failure.get("route")
    workstream = failure.get("workstream") or failure.get("workstream_id")
    if all(isinstance(value, str) and value.strip() for value in (project, route, workstream)):
        return f"{project.strip()}|{route.strip()}|{workstream.strip()}"
    if isinstance(workstream, str) and workstream.strip():
        return workstream.strip()
    return None


def collapse_failure_cascade(failures: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Read-only collapse of directly proven shared-root failure cascades.

    A root is shared only when at least two failures carry the same explicit
    structured root/evidence identity. The function never creates remediation
    work, never consumes budget, and never infers a root from textual similarity.
    """

    indexed = [dict(failure) for failure in failures]
    by_root: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, failure in enumerate(indexed):
        root = _root_identity(failure)
        if root is not None:
            by_root[root].append((index, failure))

    shared_ids = {root for root, items in by_root.items() if len(items) >= 2}
    shared_indexes = {
        index
        for root in shared_ids
        for index, _failure in by_root[root]
    }

    shared_blockers: list[dict[str, Any]] = []
    affected_lanes: dict[str, list[str]] = {}
    for root in sorted(shared_ids):
        entries = by_root[root]
        lanes = sorted(
            {
                lane
                for _index, failure in entries
                if (lane := _lane_identity(failure)) is not None
            }
        )
        affected_lanes[root] = lanes
        shared_blockers.append(
            {
                "incident_id": root,
                "failure_count": len(entries),
                "affected_lanes": lanes,
                "proof": "EXPLICIT_STRUCTURED_COMMON_ROOT_IDENTITY",
            }
        )

    unshared_failures = [
        failure
        for index, failure in enumerate(indexed)
        if index not in shared_indexes
    ]

    return {
        "schema_version": "2.1",
        "shared_blockers": shared_blockers,
        "affected_lanes": affected_lanes,
        "unshared_failures": unshared_failures,
        "correction_task_count": 0,
        "duplicate_corrections": False,
    }
