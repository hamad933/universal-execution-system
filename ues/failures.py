from __future__ import annotations

from typing import Any

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
}

CANDIDATE_STAGE_CATEGORY = {
    "code": "CANDIDATE_CODE_DEFECT",
    "lint": "CANDIDATE_CODE_DEFECT",
    "typecheck": "CANDIDATE_CODE_DEFECT",
    "test": "CANDIDATE_TEST_DEFECT",
    "format": "CANDIDATE_FORMAT_DEFECT",
    "build": "CANDIDATE_BUILD_DEFECT",
}


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
    }:
        scope = "EXTERNAL"
        blocks = []
        remediation_owner = "PLATFORM_OR_EXTERNAL_OWNER"
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
