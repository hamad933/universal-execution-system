from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlencode

from ._github_reads import _is_full_sha, _object, _require_full_sha
from .base import ProtocolError, read_json_with_retries


class GitHubEvidenceMixin:
    def get_workflow_binding(
        self,
        owner: str,
        repo: str,
        run_id: int,
        *,
        expected_sha: str,
        expected_run_attempt: int | None = None,
        required_artifacts: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        _require_full_sha(expected_sha)
        run = _object(
            self._read_json(
                f"{self._repo_path(owner, repo)}/actions/runs/{int(run_id)}",
                operation="github.workflow_run.get",
            ),
            "workflow run",
        )
        actual_attempt = int(run.get("run_attempt") or 0)
        attempt = expected_run_attempt if expected_run_attempt is not None else actual_attempt
        jobs_path = (
            f"{self._repo_path(owner, repo)}/actions/runs/{int(run_id)}/attempts/{int(attempt)}/jobs"
            if attempt
            else f"{self._repo_path(owner, repo)}/actions/runs/{int(run_id)}/jobs"
        )
        jobs = self._paginate(jobs_path, item_key="jobs", operation="github.workflow_jobs.list")
        artifacts = self._paginate(
            f"{self._repo_path(owner, repo)}/actions/runs/{int(run_id)}/artifacts",
            item_key="artifacts",
            operation="github.workflow_artifacts.list",
        )

        run_sha_match = str(run.get("head_sha") or "").lower() == expected_sha.lower()
        attempt_match = expected_run_attempt is None or actual_attempt == expected_run_attempt
        job_mismatches: list[int | None] = []
        job_ids: set[int] = set()
        for job in jobs:
            job_id = int(job.get("id") or 0)
            if job_id:
                job_ids.add(job_id)
            if int(job.get("run_id") or 0) != int(run_id) or str(job.get("head_sha") or "").lower() != expected_sha.lower():
                job_mismatches.append(job.get("id"))

        normalized_artifacts = [
            _artifact_lineage(
                artifact,
                expected_run_id=int(run_id),
                expected_attempt=int(attempt or 0),
                expected_sha=expected_sha,
                attempt_job_ids=job_ids,
            )
            for artifact in artifacts
        ]
        artifact_mismatches = [
            item["artifact_id"]
            for item in normalized_artifacts
            if item["lineage_state"] == "MISMATCH"
        ]
        artifact_unproven = [
            item["artifact_id"]
            for item in normalized_artifacts
            if item["lineage_state"] == "UNPROVEN"
        ]

        artifact_requirement_evidence = _evaluate_required_artifacts(required_artifacts, normalized_artifacts)
        if required_artifacts is None:
            artifact_binding_valid = bool(normalized_artifacts) and all(
                item["lineage_state"] == "PROVEN" for item in normalized_artifacts
            )
            required_artifact_missing: list[dict[str, Any]] = []
        else:
            artifact_binding_valid = bool(required_artifacts) and all(
                item["state"] == "PROVEN" for item in artifact_requirement_evidence
            )
            required_artifact_missing = [
                item["required"] for item in artifact_requirement_evidence if item["state"] != "PROVEN"
            ]

        base_binding_valid = run_sha_match and attempt_match and not job_mismatches
        binding_valid = base_binding_valid and artifact_binding_valid
        evidence_complete = binding_valid and bool(jobs)
        artifact_lineage_status = (
            "PROVEN"
            if artifact_binding_valid
            else "MISMATCH"
            if artifact_mismatches
            else "UNPROVEN"
        )
        return {
            "schema_version": "0.5",
            "run_id": int(run_id),
            "run_attempt": actual_attempt,
            "head_sha": run.get("head_sha"),
            "expected_sha": expected_sha,
            "run_sha_match": run_sha_match,
            "attempt_match": attempt_match,
            "jobs": jobs,
            "job_mismatches": job_mismatches,
            "artifacts": normalized_artifacts,
            "artifact_mismatches": artifact_mismatches,
            "artifact_unproven": artifact_unproven,
            "artifact_lineage_status": artifact_lineage_status,
            "required_artifact_evidence": artifact_requirement_evidence,
            "required_artifact_missing": required_artifact_missing,
            "binding_valid": binding_valid,
            "evidence_complete": evidence_complete,
            "pass_authorized": False,
        }

    def list_reviews(self, owner: str, repo: str, number: int, *, expected_sha: str) -> dict[str, Any]:
        _require_full_sha(expected_sha)
        reviews = self._paginate(
            f"{self._repo_path(owner, repo)}/pulls/{int(number)}/reviews",
            item_key=None,
            operation="github.reviews.list",
        )
        normalized: list[dict[str, Any]] = []
        complete = True
        for review in reviews:
            reviewed_sha = review.get("commit_id")
            exact = isinstance(reviewed_sha, str) and reviewed_sha.lower() == expected_sha.lower()
            if not _is_full_sha(reviewed_sha):
                complete = False
            normalized.append(
                {
                    "id": review.get("id"),
                    "state": review.get("state"),
                    "reviewed_sha": reviewed_sha,
                    "exact_sha_match": exact,
                    "stale": not exact,
                    "user": (review.get("user") or {}).get("login") if isinstance(review.get("user"), Mapping) else None,
                }
            )
        return {
            "expected_sha": expected_sha,
            "reviews": normalized,
            "evidence_complete": complete and bool(normalized),
            "all_reviews_exact_sha": bool(normalized) and all(item["exact_sha_match"] for item in normalized),
        }

    def verify_exact_head(self, owner: str, repo: str, ref: str, expected_sha: str) -> dict[str, Any]:
        _require_full_sha(expected_sha)
        live = self.get_ref_head(owner, repo, ref)
        match = live["head_sha"].lower() == expected_sha.lower()
        return {
            "ref": live["ref"],
            "expected_sha": expected_sha,
            "live_sha": live["head_sha"],
            "exact_head_match": match,
            "pass_authorized": match,
        }

    def _paginate(
        self,
        path: str,
        *,
        item_key: str | None,
        operation: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            payload = self._read_json(
                f"{path}{separator}{urlencode({'per_page': 100, 'page': page})}",
                operation=operation,
                extra_headers=extra_headers,
            )
            raw_items = payload if item_key is None else (_object(payload, "paginated response").get(item_key, []))
            if not isinstance(raw_items, list):
                raise ProtocolError("GitHub paginated response has invalid shape", operation=operation)
            for item in raw_items:
                if not isinstance(item, dict):
                    raise ProtocolError("GitHub paginated item has invalid shape", operation=operation)
                items.append(dict(item))
            if len(raw_items) < 100:
                return items
            page += 1

    def _read_json(
        self,
        path: str,
        *,
        operation: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        headers = self._headers()
        headers.update(extra_headers or {})
        return read_json_with_retries(
            self._transport,
            "GET",
            f"{self._endpoint}{path}",
            headers=headers,
            timeout=self._timeout,
            retry_policy=self._read_retry_policy,
            sleeper=self._sleeper,
            operation=operation,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @staticmethod
    def _repo_path(owner: str, repo: str) -> str:
        if not owner or not repo:
            raise ValueError("owner and repo are required")
        return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


def _artifact_lineage(
    artifact: Mapping[str, Any],
    *,
    expected_run_id: int,
    expected_attempt: int,
    expected_sha: str,
    attempt_job_ids: set[int],
) -> dict[str, Any]:
    workflow_run = artifact.get("workflow_run")
    workflow_run = workflow_run if isinstance(workflow_run, Mapping) else {}
    artifact_id = artifact.get("id")
    run_id = int(workflow_run.get("id") or artifact.get("workflow_run_id") or 0)
    head_sha = workflow_run.get("head_sha") or artifact.get("head_sha")
    run_attempt_raw = artifact.get("run_attempt")
    if run_attempt_raw is None:
        run_attempt_raw = workflow_run.get("run_attempt")
    run_attempt = int(run_attempt_raw) if isinstance(run_attempt_raw, int) or (isinstance(run_attempt_raw, str) and run_attempt_raw.isdigit()) else None
    producer_job_id = artifact.get("producer_job_id")
    producer_job = artifact.get("producer_job")
    if producer_job_id is None and isinstance(producer_job, Mapping):
        producer_job_id = producer_job.get("id")
    producer_job_id = int(producer_job_id) if isinstance(producer_job_id, int) or (isinstance(producer_job_id, str) and producer_job_id.isdigit()) else None
    run_id_match = run_id == expected_run_id
    sha_match = isinstance(head_sha, str) and head_sha.lower() == expected_sha.lower()
    attempt_match = run_attempt is not None and run_attempt == expected_attempt
    producer_match = producer_job_id is not None and producer_job_id in attempt_job_ids
    if not run_id_match or (head_sha is not None and not sha_match) or (run_attempt is not None and not attempt_match) or (producer_job_id is not None and not producer_match):
        lineage_state = "MISMATCH"
    elif run_attempt is None or producer_job_id is None or not sha_match:
        lineage_state = "UNPROVEN"
    else:
        lineage_state = "PROVEN"
    return {
        "artifact_id": artifact_id,
        "name": artifact.get("name"),
        "digest": artifact.get("digest"),
        "workflow_run_id": run_id or None,
        "run_attempt": run_attempt,
        "head_sha": head_sha,
        "producer_job_id": producer_job_id,
        "run_id_match": run_id_match,
        "attempt_match": attempt_match,
        "sha_match": sha_match,
        "producer_job_match": producer_match,
        "lineage_state": lineage_state,
    }


def _evaluate_required_artifacts(
    required: Sequence[Mapping[str, Any]] | None,
    artifacts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if required is None:
        return []
    results: list[dict[str, Any]] = []
    for raw in required:
        spec = dict(raw)
        name = spec.get("name")
        artifact_id = spec.get("artifact_id")
        digest = spec.get("digest")
        producer_job_id = spec.get("producer_job_id")
        if name is None and artifact_id is None:
            raise ValueError("required artifact specification needs name or artifact_id")
        matches = []
        for artifact in artifacts:
            if artifact_id is not None and artifact.get("artifact_id") != artifact_id:
                continue
            if name is not None and artifact.get("name") != name:
                continue
            if digest is not None and artifact.get("digest") != digest:
                continue
            if producer_job_id is not None and artifact.get("producer_job_id") != producer_job_id:
                continue
            matches.append(artifact)
        state = "MISSING" if not matches else "PROVEN" if all(item.get("lineage_state") == "PROVEN" for item in matches) else "UNPROVEN"
        results.append({"required": spec, "state": state, "match_count": len(matches)})
    return results
