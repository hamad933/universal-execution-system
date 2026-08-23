import json
import unittest

from ues.providers.base import HttpResponse, RetryPolicy
from ues.providers.github import GitHubClient


class FakeTransport:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def request(self, method, url, *, headers, body, timeout):
        self.requests.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if not self.steps:
            raise AssertionError(f"unexpected request {method} {url}")
        return self.steps.pop(0)


def response(payload, status=200):
    return HttpResponse(status=status, headers={}, body=json.dumps(payload).encode())


SHA = "a" * 40
OTHER = "b" * 40


class GitHubProviderTests(unittest.TestCase):
    def client(self, steps):
        transport = FakeTransport(steps)
        return (
            GitHubClient(
                "gh-secret-token",
                transport=transport,
                read_retry_policy=RetryPolicy(max_attempts=1),
                sleeper=lambda _: None,
            ),
            transport,
        )

    def test_missing_ci_evidence_never_passes(self):
        client, _ = self.client([response([]), response({"check_runs": []})])
        result = client.get_ci_evidence("o", "r", SHA)
        self.assertEqual(result["aggregate"], "UNKNOWN")
        self.assertFalse(result["evidence_complete"])
        self.assertFalse(result["pass_authorized"])

    def test_exact_sha_ci_can_pass_only_with_complete_success_evidence(self):
        client, _ = self.client(
            [
                response([{"id": 1, "sha": SHA, "state": "success"}]),
                response({"check_runs": [{"id": 2, "head_sha": SHA, "status": "completed", "conclusion": "success"}]}),
            ]
        )
        result = client.get_ci_evidence("o", "r", SHA)
        self.assertEqual(result["aggregate"], "PASS")
        self.assertTrue(result["pass_authorized"])

    def test_stale_artifact_run_mismatch_fails_binding(self):
        client, _ = self.client(
            [
                response({"id": 9, "head_sha": SHA, "run_attempt": 2}),
                response({"jobs": [{"id": 10, "run_id": 9, "head_sha": SHA}]}),
                response(
                    {
                        "artifacts": [
                            {"id": 11, "name": "evidence", "workflow_run": {"id": 9, "head_sha": OTHER}}
                        ]
                    }
                ),
            ]
        )
        result = client.get_workflow_binding("o", "r", 9, expected_sha=SHA, expected_run_attempt=2)
        self.assertFalse(result["binding_valid"])
        self.assertEqual(result["artifact_mismatches"], [11])
        self.assertFalse(result["pass_authorized"])

    def test_run_attempt_mismatch_fails_binding(self):
        client, _ = self.client(
            [
                response({"id": 9, "head_sha": SHA, "run_attempt": 3}),
                response({"jobs": []}),
                response({"artifacts": []}),
            ]
        )
        result = client.get_workflow_binding("o", "r", 9, expected_sha=SHA, expected_run_attempt=2)
        self.assertFalse(result["attempt_match"])
        self.assertFalse(result["binding_valid"])

    def test_exact_head_mismatch(self):
        client, _ = self.client([response({"ref": "refs/heads/feature", "object": {"type": "commit", "sha": OTHER}})])
        result = client.verify_exact_head("o", "r", "feature", SHA)
        self.assertFalse(result["exact_head_match"])
        self.assertFalse(result["pass_authorized"])

    def test_pr_binding_includes_exact_head_and_base(self):
        client, _ = self.client(
            [
                response(
                    {
                        "number": 7,
                        "state": "open",
                        "draft": True,
                        "merged": False,
                        "head": {"ref": "feature", "sha": SHA},
                        "base": {"ref": "main", "sha": OTHER},
                        "merge_commit_sha": None,
                    }
                )
            ]
        )
        result = client.get_pull_request("o", "r", 7)
        self.assertEqual(result["head_sha"], SHA)
        self.assertEqual(result["base_sha"], OTHER)
        self.assertTrue(result["draft"])

    def test_reviewed_sha_missing_is_partial_evidence(self):
        client, _ = self.client([response([{"id": 1, "state": "APPROVED", "commit_id": None}])])
        result = client.list_reviews("o", "r", 7, expected_sha=SHA)
        self.assertFalse(result["evidence_complete"])
        self.assertFalse(result["all_reviews_exact_sha"])

    def test_secret_is_not_in_repr(self):
        client, _ = self.client([])
        self.assertNotIn("gh-secret-token", repr(client))


if __name__ == "__main__":
    unittest.main()
