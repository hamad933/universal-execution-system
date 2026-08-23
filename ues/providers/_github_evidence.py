from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote, urlencode

from .base import ProtocolError, read_json_with_retries
from ._github_reads import _is_full_sha, _object, _require_full_sha


class GitHubEvidenceMixin:
        def get_workflow_binding(
            self,
            owner: str,
            repo: str,
            run_id: int,
            *,
            expected_sha: str,
            expected_run_attempt: int | None = None,
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
            for job in jobs:
                if int(job.get("run_id") or 0) != int(run_id) or str(job.get("head_sha") or "").lower() != expected_sha.lower():
                    job_mismatches.append(job.get("id"))

            artifact_mismatches: list[int | None] = []
            for artifact in artifacts:
                workflow_run = artifact.get("workflow_run")
                if not isinstance(workflow_run, Mapping):
                    artifact_mismatches.append(artifact.get("id"))
                    continue
                if int(workflow_run.get("id") or 0) != int(run_id) or str(workflow_run.get("head_sha") or "").lower() != expected_sha.lower():
                    artifact_mismatches.append(artifact.get("id"))

            binding_valid = run_sha_match and attempt_match and not job_mismatches and not artifact_mismatches
            evidence_complete = binding_valid and bool(jobs) and bool(artifacts)
            return {
                "run_id": int(run_id),
                "run_attempt": actual_attempt,
                "head_sha": run.get("head_sha"),
                "expected_sha": expected_sha,
                "run_sha_match": run_sha_match,
                "attempt_match": attempt_match,
                "jobs": jobs,
                "job_mismatches": job_mismatches,
                "artifacts": artifacts,
                "artifact_mismatches": artifact_mismatches,
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


