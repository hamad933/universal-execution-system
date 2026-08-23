from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from ._github_reads import _require_full_sha

_FAILURE_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "stale", "startup_failure"}
_PENDING_STATES = {"pending", "queued", "in_progress", "requested", "waiting"}


class GitHubCIMixin:
    def get_ci_evidence(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        """Return unscoped CI observations; never authorize PASS without a required-CI contract."""
        _require_full_sha(sha)
        statuses, checks = self._read_commit_statuses_and_checks(owner, repo, sha)
        exact = all(str(item.get("sha") or "").lower() == sha.lower() for item in statuses)
        exact = exact and all(str(item.get("head_sha") or "").lower() == sha.lower() for item in checks)
        observed = _aggregate_observed(statuses, checks, sha)
        return {
            "sha": sha,
            "exact_sha_match": exact,
            "evidence_complete": False,
            "aggregate": "UNKNOWN",
            "observed_aggregate": observed,
            "verdict": "NOT_A_PASS",
            "reason": "REQUIRED_CI_SPEC_NOT_EVALUATED",
            "required_ci_evaluated": False,
            "status_count": len(statuses),
            "check_count": len(checks),
            "pass_authorized": False,
            "statuses": statuses,
            "check_runs": checks,
        }

    def get_required_ci_evidence(
        self,
        owner: str,
        repo: str,
        sha: str,
        required_checks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Evaluate exact-SHA CI only against adapter/integration supplied required identities.

        Supported kinds:
        - {"kind": "status", "context": "..."}
        - {"kind": "check", "name": "..."}
        - {"kind": "workflow", "workflow": <name|path|workflow_id>}
        - {"kind": "job", "workflow": <name|path|workflow_id>, "job": "..."}
        """
        _require_full_sha(sha)
        specs = [_normalize_required_spec(spec) for spec in required_checks]
        if not specs:
            return _required_result(sha, [], reason="REQUIRED_CI_SPEC_MISSING")

        needs_commit = any(spec["kind"] in {"status", "check"} for spec in specs)
        statuses: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        if needs_commit:
            statuses, checks = self._read_commit_statuses_and_checks(owner, repo, sha)

        needs_runs = any(spec["kind"] in {"workflow", "job"} for spec in specs)
        runs: list[dict[str, Any]] = []
        if needs_runs:
            path = f"{self._repo_path(owner, repo)}/actions/runs?{urlencode({'head_sha': sha})}"
            runs = self._paginate(path, item_key="workflow_runs", operation="github.workflow_runs.list")

        evidence: list[dict[str, Any]] = []
        job_cache: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for spec in specs:
            kind = spec["kind"]
            if kind == "status":
                matches = [item for item in statuses if str(item.get("context") or "") == spec["context"]]
                evidence.append(_evaluate_status_spec(spec, matches, sha))
            elif kind == "check":
                matches = [item for item in checks if str(item.get("name") or "") == spec["name"]]
                evidence.append(_evaluate_check_spec(spec, matches, sha))
            elif kind == "workflow":
                matches = [item for item in runs if _workflow_matches(item, spec["workflow"])]
                evidence.append(_evaluate_workflow_spec(spec, matches, sha))
            else:
                matched_runs = [item for item in runs if _workflow_matches(item, spec["workflow"])]
                job_matches: list[dict[str, Any]] = []
                stale_run = False
                for run in matched_runs:
                    run_sha = str(run.get("head_sha") or "")
                    if run_sha.lower() != sha.lower():
                        stale_run = True
                        continue
                    run_id = int(run.get("id") or 0)
                    attempt = int(run.get("run_attempt") or 0)
                    if not run_id or not attempt:
                        continue
                    key = (run_id, attempt)
                    if key not in job_cache:
                        job_cache[key] = self._paginate(
                            f"{self._repo_path(owner, repo)}/actions/runs/{run_id}/attempts/{attempt}/jobs",
                            item_key="jobs",
                            operation="github.workflow_jobs.list",
                        )
                    for job in job_cache[key]:
                        if str(job.get("name") or "") == spec["job"]:
                            candidate = dict(job)
                            candidate["_run_head_sha"] = run_sha
                            candidate["_run_id"] = run_id
                            candidate["_run_attempt"] = attempt
                            job_matches.append(candidate)
                item = _evaluate_job_spec(spec, job_matches, sha)
                if stale_run and item["state"] == "MISSING":
                    item["state"] = "STALE_OR_MISMATCH"
                evidence.append(item)

        reason = _overall_required_reason(evidence)
        passed = reason == "ALL_REQUIRED_CI_SATISFIED"
        return {
            "schema_version": "0.5",
            "sha": sha,
            "required_ci_evaluated": True,
            "required_count": len(specs),
            "required_evidence": evidence,
            "verdict": "PASS" if passed else "NOT_A_PASS",
            "reason": reason,
            "evidence_complete": all(item["state"] not in {"MISSING", "UNKNOWN", "STALE_OR_MISMATCH"} for item in evidence),
            "exact_sha_match": all(item["exact_sha_match"] for item in evidence),
            "pass_authorized": passed,
        }

    def _read_commit_statuses_and_checks(self, owner: str, repo: str, sha: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        statuses = self._paginate(
            f"{self._repo_path(owner, repo)}/commits/{sha}/statuses",
            item_key=None,
            operation="github.statuses.list",
        )
        checks = self._paginate(
            f"{self._repo_path(owner, repo)}/commits/{sha}/check-runs",
            item_key="check_runs",
            operation="github.checks.list",
            extra_headers={"Accept": "application/vnd.github+json"},
        )
        return statuses, checks


def _normalize_required_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise ValueError("required CI specification items must be mappings")
    kind = str(spec.get("kind") or "").strip().lower()
    if kind == "status":
        context = str(spec.get("context") or "").strip()
        if not context: raise ValueError("required status specification needs context")
        return {"kind": kind, "context": context}
    if kind == "check":
        name = str(spec.get("name") or "").strip()
        if not name: raise ValueError("required check specification needs name")
        return {"kind": kind, "name": name}
    if kind == "workflow":
        workflow = spec.get("workflow")
        if workflow in {None, ""}: raise ValueError("required workflow specification needs workflow identity")
        return {"kind": kind, "workflow": workflow}
    if kind == "job":
        workflow = spec.get("workflow"); job = str(spec.get("job") or "").strip()
        if workflow in {None, ""} or not job: raise ValueError("required job specification needs workflow and job identities")
        return {"kind": kind, "workflow": workflow, "job": job}
    raise ValueError(f"unsupported required CI kind: {kind or '<missing>'}")


def _aggregate_observed(statuses: Sequence[Mapping[str, Any]], checks: Sequence[Mapping[str, Any]], sha: str) -> str:
    evidence_count = len(statuses) + len(checks)
    if not evidence_count: return "UNKNOWN"
    if any(str(item.get("sha") or "").lower() != sha.lower() for item in statuses): return "STALE_OR_MISMATCH"
    if any(str(item.get("head_sha") or "").lower() != sha.lower() for item in checks): return "STALE_OR_MISMATCH"
    if any(str(item.get("state") or "").lower() in {"error", "failure"} for item in statuses): return "FAILURE"
    if any(str(item.get("conclusion") or "").lower() in _FAILURE_CONCLUSIONS for item in checks): return "FAILURE"
    if any(str(item.get("state") or "").lower() in _PENDING_STATES for item in statuses): return "PENDING"
    if any(str(item.get("status") or "").lower() in _PENDING_STATES for item in checks): return "PENDING"
    if all(str(item.get("state") or "").lower() == "success" for item in statuses) and all(
        str(item.get("status") or "").lower() == "completed" and str(item.get("conclusion") or "").lower() == "success" for item in checks
    ):
        return "PASS"
    return "UNKNOWN"


def _evaluate_status_spec(spec: Mapping[str, Any], matches: Sequence[Mapping[str, Any]], sha: str) -> dict[str, Any]:
    states = [str(item.get("state") or "").lower() for item in matches]
    exact = bool(matches) and all(str(item.get("sha") or "").lower() == sha.lower() for item in matches)
    state = _state_from_matches(matches, exact, states, success={"success"}, failure={"failure", "error"})
    return {"required": dict(spec), "state": state, "exact_sha_match": exact, "match_count": len(matches)}


def _evaluate_check_spec(spec: Mapping[str, Any], matches: Sequence[Mapping[str, Any]], sha: str) -> dict[str, Any]:
    exact = bool(matches) and all(str(item.get("head_sha") or "").lower() == sha.lower() for item in matches)
    states=[]
    for item in matches:
        status=str(item.get("status") or "").lower(); conclusion=str(item.get("conclusion") or "").lower()
        states.append("success" if status=="completed" and conclusion=="success" else "failure" if conclusion in _FAILURE_CONCLUSIONS else "pending" if status in _PENDING_STATES else "unknown")
    state=_state_from_matches(matches,exact,states,success={"success"},failure={"failure"})
    return {"required":dict(spec),"state":state,"exact_sha_match":exact,"match_count":len(matches)}


def _evaluate_workflow_spec(spec: Mapping[str, Any], matches: Sequence[Mapping[str, Any]], sha: str) -> dict[str, Any]:
    exact=bool(matches) and all(str(item.get("head_sha") or "").lower()==sha.lower() for item in matches); states=[]
    for item in matches:
        status=str(item.get("status") or "").lower(); conclusion=str(item.get("conclusion") or "").lower()
        states.append("success" if status=="completed" and conclusion=="success" else "failure" if conclusion in _FAILURE_CONCLUSIONS else "pending" if status in _PENDING_STATES else "unknown")
    state=_state_from_matches(matches,exact,states,success={"success"},failure={"failure"})
    return {"required":dict(spec),"state":state,"exact_sha_match":exact,"match_count":len(matches)}


def _evaluate_job_spec(spec: Mapping[str, Any], matches: Sequence[Mapping[str, Any]], sha: str) -> dict[str, Any]:
    exact=bool(matches) and all(str(item.get("head_sha") or item.get("_run_head_sha") or "").lower()==sha.lower() for item in matches); states=[]
    for item in matches:
        status=str(item.get("status") or "").lower(); conclusion=str(item.get("conclusion") or "").lower()
        states.append("success" if status=="completed" and conclusion=="success" else "failure" if conclusion in _FAILURE_CONCLUSIONS else "pending" if status in _PENDING_STATES else "unknown")
    state=_state_from_matches(matches,exact,states,success={"success"},failure={"failure"})
    return {"required":dict(spec),"state":state,"exact_sha_match":exact,"match_count":len(matches)}


def _state_from_matches(matches: Sequence[Mapping[str, Any]], exact: bool, states: Sequence[str], *, success:set[str], failure:set[str])->str:
    if not matches: return "MISSING"
    if not exact: return "STALE_OR_MISMATCH"
    if any(value in failure for value in states): return "FAILURE"
    if any(value=="pending" for value in states): return "PENDING"
    if states and all(value in success for value in states): return "SUCCESS"
    return "UNKNOWN"


def _workflow_matches(run: Mapping[str, Any], identity: Any) -> bool:
    if isinstance(identity, int): return int(run.get("workflow_id") or 0)==identity
    wanted=str(identity).strip()
    return wanted in {str(run.get("name") or ""),str(run.get("path") or ""),str(run.get("workflow_id") or "")}


def _overall_required_reason(evidence: Sequence[Mapping[str, Any]]) -> str:
    states={str(item.get("state") or "UNKNOWN") for item in evidence}
    if "MISSING" in states: return "REQUIRED_CI_MISSING"
    if "STALE_OR_MISMATCH" in states: return "REQUIRED_CI_STALE_OR_MISMATCH"
    if "FAILURE" in states: return "REQUIRED_CI_FAILED"
    if "PENDING" in states: return "REQUIRED_CI_PENDING"
    if "UNKNOWN" in states: return "REQUIRED_CI_UNPROVEN"
    if states=={"SUCCESS"}: return "ALL_REQUIRED_CI_SATISFIED"
    return "REQUIRED_CI_UNPROVEN"


def _required_result(sha:str,evidence:list[dict[str,Any]],*,reason:str)->dict[str,Any]:
    return {"schema_version":"0.5","sha":sha,"required_ci_evaluated":True,"required_count":0,"required_evidence":evidence,"verdict":"NOT_A_PASS","reason":reason,"evidence_complete":False,"exact_sha_match":False,"pass_authorized":False}
