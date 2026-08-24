from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlencode

from ._github_reads import _require_full_sha
from .base import NetworkError, ProtocolError, WriteOutcomeUnknown, encode_json, error_for_response


class GitHubDispatchMixin:
    def dispatch_workflow_bounded(
        self,
        owner: str,
        repo: str,
        *,
        workflow: str,
        ref: str,
        expected_sha: str,
        inputs: Mapping[str, str] | None,
        allowed_workflows: Sequence[str],
        allowed_inputs: Mapping[str, Sequence[str]],
        purpose: str,
    ) -> dict[str, Any]:
        """Dispatch one explicitly allowlisted workflow and prove its run binding.

        This method deliberately exposes no arbitrary workflow surface. The
        caller supplies the exact workflow allowlist and each input's allowed
        values from current project authority/stable policy.
        """

        _require_full_sha(expected_sha)
        workflow_name = str(workflow or "").strip()
        ref_name = str(ref or "").strip().removeprefix("refs/heads/")
        purpose_text = str(purpose or "").strip()
        if not workflow_name or workflow_name not in {str(x) for x in allowed_workflows}:
            raise ValueError("workflow is not explicitly allowlisted")
        if not ref_name or not purpose_text:
            raise ValueError("exact ref and purpose are required")

        normalized_inputs: dict[str, str] = {}
        supplied = dict(inputs or {})
        unknown_keys = sorted(set(supplied) - set(allowed_inputs))
        if unknown_keys:
            raise ValueError(f"workflow input keys are not allowlisted: {', '.join(unknown_keys)}")
        for key, value in supplied.items():
            text = str(value)
            allowed_values = {str(item) for item in allowed_inputs.get(key, ())}
            if text not in allowed_values:
                raise ValueError(f"workflow input value is not allowlisted: {key}")
            normalized_inputs[str(key)] = text

        live = self.get_ref_head(owner, repo, ref_name)
        if str(live.get("head_sha") or "").lower() != expected_sha.lower():
            raise ProtocolError(
                "workflow dispatch ref moved from expected exact SHA",
                operation="github.workflow_dispatch.preflight",
            )

        workflow_path = quote(workflow_name, safe="")
        query = urlencode({"event": "workflow_dispatch", "branch": ref_name, "per_page": 100})
        runs_path = f"{self._repo_path(owner, repo)}/actions/workflows/{workflow_path}/runs?{query}"
        before = self._read_json(runs_path, operation="github.workflow_dispatch.pre_runs")
        before_items = before.get("workflow_runs", []) if isinstance(before, Mapping) else []
        if not isinstance(before_items, list):
            raise ProtocolError("workflow run pre-readback has invalid shape", operation="github.workflow_dispatch.pre_runs")
        before_ids = {int(item.get("id") or 0) for item in before_items if isinstance(item, Mapping) and item.get("id")}

        body = {"ref": ref_name, "inputs": normalized_inputs}
        try:
            response = self._transport.request(
                "POST",
                f"{self._endpoint}{self._repo_path(owner, repo)}/actions/workflows/{workflow_path}/dispatches",
                headers={**self._headers(), "Content-Type": "application/json"},
                body=encode_json(body),
                timeout=self._timeout,
            )
        except NetworkError as exc:
            raise WriteOutcomeUnknown(
                "GitHub workflow dispatch transport result is unknown",
                operation="github.workflow_dispatch",
                recovery={"verdict": "LIST_WORKFLOW_RUNS_BEFORE_RETRY", "safe_to_blind_retry": False},
            ) from exc

        if not 200 <= response.status <= 299:
            raise error_for_response(response, operation="github.workflow_dispatch")

        after = self._read_json(runs_path, operation="github.workflow_dispatch.post_runs")
        after_items = after.get("workflow_runs", []) if isinstance(after, Mapping) else []
        if not isinstance(after_items, list):
            raise WriteOutcomeUnknown(
                "workflow dispatch succeeded but run readback shape is invalid",
                operation="github.workflow_dispatch",
                recovery={"verdict": "LIST_WORKFLOW_RUNS_BEFORE_RETRY", "safe_to_blind_retry": False},
            )

        candidates: list[Mapping[str, Any]] = []
        for item in after_items:
            if not isinstance(item, Mapping):
                continue
            run_id = int(item.get("id") or 0)
            if not run_id or run_id in before_ids:
                continue
            if str(item.get("event") or "") != "workflow_dispatch":
                continue
            if str(item.get("head_sha") or "").lower() != expected_sha.lower():
                continue
            head_branch = str(item.get("head_branch") or "")
            if head_branch and head_branch != ref_name:
                continue
            candidates.append(item)

        if len(candidates) != 1:
            raise WriteOutcomeUnknown(
                "workflow dispatch succeeded but exact new run could not be uniquely read back",
                operation="github.workflow_dispatch",
                recovery={
                    "verdict": "LIST_WORKFLOW_RUNS_BEFORE_RETRY",
                    "candidate_count": len(candidates),
                    "safe_to_blind_retry": False,
                },
            )

        run = candidates[0]
        run_id = int(run.get("id") or 0)
        attempt = int(run.get("run_attempt") or 1)
        readback = self._read_json(
            f"{self._repo_path(owner, repo)}/actions/runs/{run_id}",
            operation="github.workflow_dispatch.run_readback",
        )
        if not isinstance(readback, Mapping):
            raise WriteOutcomeUnknown(
                "workflow dispatch run readback is not an object",
                operation="github.workflow_dispatch",
                recovery={"verdict": "READ_RUN_BEFORE_RETRY", "safe_to_blind_retry": False},
            )
        if (
            int(readback.get("id") or 0) != run_id
            or str(readback.get("event") or "") != "workflow_dispatch"
            or str(readback.get("head_sha") or "").lower() != expected_sha.lower()
        ):
            raise WriteOutcomeUnknown(
                "workflow dispatch authoritative run binding mismatch",
                operation="github.workflow_dispatch",
                recovery={"verdict": "READ_RUN_BEFORE_RETRY", "safe_to_blind_retry": False},
            )

        return {
            "provider": "GITHUB",
            "operation": "workflow_dispatch",
            "repository": f"{owner}/{repo}",
            "workflow": workflow_name,
            "ref": ref_name,
            "head_sha": expected_sha,
            "inputs": normalized_inputs,
            "purpose": purpose_text,
            "run_id": run_id,
            "run_attempt": int(readback.get("run_attempt") or attempt),
            "event": "workflow_dispatch",
            "authoritative_readback": True,
            "safe_to_blind_retry": False,
        }
