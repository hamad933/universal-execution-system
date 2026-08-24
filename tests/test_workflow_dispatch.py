from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ues.providers.base import HttpResponse, NetworkError, WriteOutcomeUnknown
from ues.providers.github import GitHubClient
from ues.state_store import DeterministicFileStateStore
from ues.workflow_dispatch import dispatch_workflow_once


class DispatchTransport:
    def __init__(self, *, network_unknown: bool = False, ambiguous: bool = False):
        self.network_unknown = network_unknown
        self.ambiguous = ambiguous
        self.post_count = 0
        self.pre_read = True

    def request(self, method, url, *, headers, body, timeout):
        if method == "GET" and "/git/ref/heads/" in url:
            return self.json({"ref": "refs/heads/work/w05", "object": {"type": "commit", "sha": "a" * 40}})
        if method == "GET" and "/actions/workflows/" in url and "/runs?" in url:
            if self.pre_read:
                self.pre_read = False
                return self.json({"workflow_runs": []})
            runs = [self.run(101)]
            if self.ambiguous:
                runs.append(self.run(102))
            return self.json({"workflow_runs": runs})
        if method == "POST" and url.endswith("/dispatches"):
            self.post_count += 1
            if self.network_unknown:
                raise NetworkError("simulated unknown")
            payload = json.loads((body or b"{}").decode("utf-8"))
            if payload != {"ref": "work/w05", "inputs": {"route_profiles": "W05"}}:
                return HttpResponse(422, {}, b"")
            return HttpResponse(204, {}, b"")
        if method == "GET" and "/actions/runs/101" in url:
            return self.json(self.run(101))
        raise AssertionError(f"unexpected request: {method} {url}")

    @staticmethod
    def run(run_id):
        return {
            "id": run_id,
            "event": "workflow_dispatch",
            "head_sha": "a" * 40,
            "head_branch": "work/w05",
            "run_attempt": 1,
        }

    @staticmethod
    def json(value):
        return HttpResponse(200, {"Content-Type": "application/json"}, json.dumps(value).encode("utf-8"))


class FakeDispatchGitHub:
    def __init__(self):
        self.calls = 0

    def dispatch_workflow_bounded(self, owner, repo, **kwargs):
        self.calls += 1
        return {
            "repository": f"{owner}/{repo}",
            "workflow": kwargs["workflow"],
            "ref": kwargs["ref"],
            "head_sha": kwargs["expected_sha"],
            "run_id": 101,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "authoritative_readback": True,
        }


class WorkflowDispatchTests(unittest.TestCase):
    def test_w05_exact_profile_dispatch_is_read_back(self):
        transport = DispatchTransport()
        client = GitHubClient("token", transport=transport, sleeper=lambda _: None)
        receipt = client.dispatch_workflow_bounded(
            "hamad933",
            "Cybersecurity-Education-Platform",
            workflow=".github/workflows/release-verification.yml",
            ref="work/w05",
            expected_sha="a" * 40,
            inputs={"route_profiles": "W05"},
            allowed_workflows=[".github/workflows/release-verification.yml"],
            allowed_inputs={"route_profiles": ["W05"]},
            purpose="W05_ROUTE_SPECIFIC_BROWSER_EVIDENCE",
        )
        self.assertEqual(receipt["event"], "workflow_dispatch")
        self.assertEqual(receipt["run_id"], 101)
        self.assertEqual(receipt["head_sha"], "a" * 40)
        self.assertEqual(transport.post_count, 1)

    def test_non_allowlisted_profile_is_rejected_before_post(self):
        transport = DispatchTransport()
        client = GitHubClient("token", transport=transport, sleeper=lambda _: None)
        with self.assertRaises(ValueError):
            client.dispatch_workflow_bounded(
                "hamad933",
                "Cybersecurity-Education-Platform",
                workflow=".github/workflows/release-verification.yml",
                ref="work/w05",
                expected_sha="a" * 40,
                inputs={"route_profiles": "ADMIN"},
                allowed_workflows=[".github/workflows/release-verification.yml"],
                allowed_inputs={"route_profiles": ["W05"]},
                purpose="W05_ROUTE_SPECIFIC_BROWSER_EVIDENCE",
            )
        self.assertEqual(transport.post_count, 0)

    def test_ambiguous_dispatch_requires_read_before_retry(self):
        transport = DispatchTransport(ambiguous=True)
        client = GitHubClient("token", transport=transport, sleeper=lambda _: None)
        with self.assertRaises(WriteOutcomeUnknown) as caught:
            client.dispatch_workflow_bounded(
                "hamad933",
                "Cybersecurity-Education-Platform",
                workflow=".github/workflows/release-verification.yml",
                ref="work/w05",
                expected_sha="a" * 40,
                inputs={"route_profiles": "W05"},
                allowed_workflows=[".github/workflows/release-verification.yml"],
                allowed_inputs={"route_profiles": ["W05"]},
                purpose="W05_ROUTE_SPECIFIC_BROWSER_EVIDENCE",
            )
        self.assertEqual(caught.exception.recovery["verdict"], "LIST_WORKFLOW_RUNS_BEFORE_RETRY")
        self.assertFalse(caught.exception.recovery["safe_to_blind_retry"])

    def test_dispatch_effect_is_durably_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeterministicFileStateStore(Path(directory) / "state.json")
            store.initialize()
            github = FakeDispatchGitHub()
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
            second = dispatch_workflow_once(**args)
            self.assertEqual(first["decision"], "WORKFLOW_DISPATCH_CONFIRMED")
            self.assertEqual(second["decision"], "IDEMPOTENT_REPLAY_CONFIRMED")
            self.assertEqual(github.calls, 1)


if __name__ == "__main__":
    unittest.main()
