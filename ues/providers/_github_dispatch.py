from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlencode

from ._github_reads import _require_full_sha
from .base import NetworkError, ProtocolError, WriteOutcomeUnknown, encode_json, error_for_response


class GitHubDispatchMixin:
    def _workflow_dispatch_runs(
        self,
        owner: str,
        repo: str,
        *,
        workflow: str,
        ref: str,
        operation: str,
    ) -> list[Mapping[str, Any]]:
        workflow_path = quote(str(workflow), safe="")
        query = urlencode({"event": "workflow_dispatch", "branch": ref, "per_page": 100})
        payload = self._read_json(
            f"{self._repo_path(owner, repo)}/actions/workflows/{workflow_path}/runs?{query}",
            operation=operation,
        )
        items = payload.get("workflow_runs", []) if isinstance(payload, Mapping) else []
        if not isinstance(items, list):
            raise ProtocolError("workflow run readback has invalid shape", operation=operation)
        return [item for item in items if isinstance(item, Mapping)]

    @staticmethod
    def _dispatch_candidates(
        items: Sequence[Mapping[str, Any]],
        *,
        before_run_ids: set[int],
        ref: str,
        expected_sha: str,
    ) -> list[Mapping[str, Any]]:
        candidates: list[Mapping[str, Any]] = []
        for item in items:
            run_id = int(item.get("id") or 0)
            if not run_id or run_id in before_run_ids:
                continue
            if str(item.get("event") or "") != "workflow_dispatch":
                continue
            if str(item.get("head_sha") or "").lower() != expected_sha.lower():
                continue
            head_branch = str(item.get("head_branch") or "")
            if head_branch and head_branch != ref:
                continue
            candidates.append(item)
        return candidates

    def _read_exact_dispatch_run(
        self,
        owner: str,
        repo: str,
        *,
        run_id: int,
        expected_sha: str,
        expected_ref: str,
        operation: str,
    ) -> dict[str, Any]:
        readback = self._read_json(
            f"{self._repo_path(owner, repo)}/actions/runs/{int(run_id)}",
            operation=operation,
        )
        if not isinstance(readback, Mapping):
            raise WriteOutcomeUnknown(
                "workflow dispatch run readback is not an object",
                operation=operation,
                recovery={"verdict": "READ_RUN_BEFORE_RETRY", "safe_to_blind_retry": False},
            )
        head_branch = str(readback.get("head_branch") or "")
        if (
            int(readback.get("id") or 0) != int(run_id)
            or str(readback.get("event") or "") != "workflow_dispatch"
            or str(readback.get("head_sha") or "").lower() != expected_sha.lower()
            or (head_branch and head_branch != expected_ref)
        ):
            raise WriteOutcomeUnknown(
                "workflow dispatch authoritative run binding mismatch",
                operation=operation,
                recovery={"verdict": "READ_RUN_BEFORE_RETRY", "safe_to_blind_retry": False},
            )
        return dict(readback)

    def reconcile_workflow_dispatch_bounded(
        self,
        owner: str,
        repo: str,
        *,
        workflow: str,
        ref: str,
        expected_sha: str,
        before_run_ids: Sequence[int],
    ) -> dict[str, Any]:
        """Resolve an UNKNOWN dispatch without sending another POST."""

        _require_full_sha(expected_sha)
        ref_name = str(ref or "").strip().removeprefix("refs/heads/")
        prior = {int(item) for item in before_run_ids}
        items = self._workflow_dispatch_runs(
            owner,
            repo,
            workflow=workflow,
            ref=ref_name,
            operation="github.workflow_dispatch.reconcile_runs",
        )
        candidates = self._dispatch_candidates(
            items,
            before_run_ids=prior,
            ref=ref_name,
            expected_sha=expected_sha,
        )
        if not candidates:
            return {
                "decision": "WORKFLOW_DISPATCH_UNKNOWN_NOT_YET_OBSERVED",
                "match_count": 0,
                "safe_to_blind_retry": False,
            }
        if len(candidates) != 1:
            return {
                "decision": "WORKFLOW_DISPATCH_RECONCILIATION_AMBIGUOUS",
                "match_count": len(candidates),
                "safe_to_blind_retry": False,
            }
        run_id = int(candidates[0].get("id") or 0)
        run = self._read_exact_dispatch_run(
            owner,
            repo,
            run_id=run_id,
            expected_sha=expected_sha,
            expected_ref=ref_name,
            operation="github.workflow_dispatch.reconcile_run_readback",
        )
        return {
            "decision": "WORKFLOW_DISPATCH_AUTHORITATIVELY_RECONCILED",
            "match_count": 1,
            "run_id": run_id,
            "run_attempt": int(run.get("run_attempt") or 1),
            "event": "workflow_dispatch",
            "head_sha": expected_sha,
            "ref": ref_name,
            "safe_to_blind_retry": False,
        }

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
        """Dispatch one explicitly allowlisted workflow and prove its run binding."""

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

        before_items = self._workflow_dispatch_runs(
            owner,
            repo,
            workflow=workflow_name,
            ref=ref_name,
            operation="github.workflow_dispatch.pre_runs",
        )
        before_ids = {int(item.get("id") or 0) for item in before_items if item.get("id")}

        workflow_path = quote(workflow_name, safe="")
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
                recovery={
                    "verdict": "LIST_WORKFLOW_RUNS_BEFORE_RETRY",
                    "before_run_ids": sorted(before_ids),
                    "safe_to_blind_retry": False,
                },
            ) from exc

        if not 200 <= response.status <= 299:
            raise error_for_response(response, operation="github.workflow_dispatch")

        try:
            after_items = self._workflow_dispatch_runs(
                owner,
                repo,
                workflow=workflow_name,
                ref=ref_name,
                operation="github.workflow_dispatch.post_runs",
            )
        except ProtocolError as exc:
            raise WriteOutcomeUnknown(
                "workflow dispatch succeeded but run readback shape is invalid",
                operation="github.workflow_dispatch",
                recovery={
                    "verdict": "LIST_WORKFLOW_RUNS_BEFORE_RETRY",
                    "before_run_ids": sorted(before_ids),
                    "safe_to_blind_retry": False,
                },
            ) from exc

        candidates = self._dispatch_candidates(
            after_items,
            before_run_ids=before_ids,
            ref=ref_name,
            expected_sha=expected_sha,
        )
        if len(candidates) != 1:
            raise WriteOutcomeUnknown(
                "workflow dispatch succeeded but exact new run could not be uniquely read back",
                operation="github.workflow_dispatch",
                recovery={
                    "verdict": "LIST_WORKFLOW_RUNS_BEFORE_RETRY",
                    "candidate_count": len(candidates),
                    "before_run_ids": sorted(before_ids),
                    "safe_to_blind_retry": False,
                },
            )

        run_id = int(candidates[0].get("id") or 0)
        try:
            readback = self._read_exact_dispatch_run(
                owner,
                repo,
                run_id=run_id,
                expected_sha=expected_sha,
                expected_ref=ref_name,
                operation="github.workflow_dispatch.run_readback",
            )
        except WriteOutcomeUnknown as exc:
            recovery = dict(exc.recovery)
            recovery["before_run_ids"] = sorted(before_ids)
            raise WriteOutcomeUnknown(
                str(exc),
                operation="github.workflow_dispatch",
                recovery=recovery,
            ) from exc

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
            "run_attempt": int(readback.get("run_attempt") or 1),
            "event": "workflow_dispatch",
            "before_run_ids": sorted(before_ids),
            "authoritative_readback": True,
            "safe_to_blind_retry": False,
        }
