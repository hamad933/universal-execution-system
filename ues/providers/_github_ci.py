from __future__ import annotations

from typing import Any

from ._github_reads import _require_full_sha


class GitHubCIMixin:
        def get_ci_evidence(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
            _require_full_sha(sha)
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

            exact = all(str(item.get("sha") or "").lower() == sha.lower() for item in statuses)
            exact = exact and all(str(item.get("head_sha") or "").lower() == sha.lower() for item in checks)
            evidence_count = len(statuses) + len(checks)
            missing = evidence_count == 0
            status_states = [str(item.get("state") or "").lower() for item in statuses]
            check_states = [str(item.get("status") or "").lower() for item in checks]
            check_conclusions = [str(item.get("conclusion") or "").lower() for item in checks]

            if not exact:
                aggregate = "STALE_OR_MISMATCH"
            elif missing:
                aggregate = "UNKNOWN"
            elif any(state in {"error", "failure"} for state in status_states) or any(
                conclusion in {"failure", "cancelled", "timed_out", "action_required", "stale"}
                for conclusion in check_conclusions
            ):
                aggregate = "FAILURE"
            elif any(state in {"pending", "queued", "in_progress", "requested", "waiting"} for state in status_states + check_states):
                aggregate = "PENDING"
            elif all(state == "success" for state in status_states) and all(
                status == "completed" and conclusion == "success"
                for status, conclusion in zip(check_states, check_conclusions, strict=True)
            ):
                aggregate = "PASS"
            else:
                aggregate = "UNKNOWN"

            return {
                "sha": sha,
                "exact_sha_match": exact,
                "evidence_complete": exact and not missing and aggregate != "UNKNOWN",
                "aggregate": aggregate,
                "status_count": len(statuses),
                "check_count": len(checks),
                "pass_authorized": aggregate == "PASS" and exact and not missing,
                "statuses": statuses,
                "check_runs": checks,
            }

