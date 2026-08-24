from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ues.providers.base import HttpResponse, NetworkError
from ues.providers.github import GitHubClient
from ues.state_store import DeterministicFileStateStore
from ues.workflow_dispatch import dispatch_workflow_once, reconcile_unknown_workflow_dispatch


class LostDispatchResponseTransport:
    def __init__(self) -> None:
        self.post_count = 0
        self.run_list_count = 0

    def request(self, method, url, *, headers, body, timeout):
        if method == "GET" and "/git/ref/heads/" in url:
            return self.json({"ref": "refs/heads/work/w05", "object": {"type": "commit", "sha": "a" * 40}})
        if method == "GET" and "/actions/workflows/" in url and "/runs?" in url:
            self.run_list_count += 1
            if self.run_list_count == 1:
                return self.json({"workflow_runs": []})
            return self.json({"workflow_runs": [self.run()]})
        if method == "POST" and url.endswith("/dispatches"):
            self.post_count += 1
            raise NetworkError("response lost after provider accepted request")
        if method == "GET" and "/actions/runs/501" in url:
            return self.json(self.run())
        raise AssertionError(f"unexpected request: {method} {url}")

    @staticmethod
    def run():
        return {
            "id": 501,
            "event": "workflow_dispatch",
            "head_sha": "a" * 40,
            "head_branch": "work/w05",
            "run_attempt": 1,
        }

    @staticmethod
    def json(value):
        return HttpResponse(200, {"Content-Type": "application/json"}, json.dumps(value).encode("utf-8"))


class WorkflowDispatchReconciliationTests(unittest.TestCase):
    def test_lost_dispatch_response_is_read_back_without_second_post(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DeterministicFileStateStore(Path(directory) / "state.json")
            store.initialize()
            transport = LostDispatchResponseTransport()
            github = GitHubClient("token", transport=transport, sleeper=lambda _: None)
            args = dict(
                store=store,
                github=github,
                project="CEP",
                route="PERSONAL:CEP",
                workstream="W05-EVIDENCE",
                owner="hamad933",
                repo="Cybersecurity-Education-Platform",
                workflow=".github/workflows/release-verification.yml",
                ref="work/w05",
                expected_sha="a" * 40,
                inputs={"route_profiles": "W05"},
                allowed_workflows=[".github/workflows/release-verification.yml"],
                allowed_inputs={"route_profiles": ["W05"]},
                purpose="W05_ROUTE_SPECIFIC_BROWSER_EVIDENCE",
                authority_event_id="CEP-W05-EVIDENCE-AUTH",
            )
            first = dispatch_workflow_once(**args)
            self.assertEqual(first["decision"], "WORKFLOW_DISPATCH_OUTCOME_UNKNOWN_RECONCILIATION_REQUIRED")
            self.assertEqual(transport.post_count, 1)

            reconciled = reconcile_unknown_workflow_dispatch(
                store,
                github,
                project="CEP",
                route="PERSONAL:CEP",
                workstream="W05-EVIDENCE",
                owner="hamad933",
                repo="Cybersecurity-Education-Platform",
                workflow=".github/workflows/release-verification.yml",
                ref="work/w05",
                expected_sha="a" * 40,
                inputs={"route_profiles": "W05"},
                purpose="W05_ROUTE_SPECIFIC_BROWSER_EVIDENCE",
            )
            self.assertEqual(reconciled["decision"], "WORKFLOW_DISPATCH_UNKNOWN_AUTHORITATIVELY_RECONCILED")
            self.assertEqual(reconciled["run_id"], 501)
            self.assertEqual(transport.post_count, 1)


if __name__ == "__main__":
    unittest.main()
