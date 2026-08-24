from __future__ import annotations

import json
import unittest

from ues.jules_lifecycle import JulesLifecycleClient, terminal_session_continuation_supported
from ues.providers.base import HttpResponse


class CreateSessionTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes | None]] = []

    def request(self, method, url, *, headers, body, timeout):
        self.requests.append((method, url, body))
        if method == "GET" and url.endswith("/v1alpha/sources/github/owner/repo"):
            payload = {"name": "sources/github/owner/repo", "githubRepo": {"owner": "owner", "repo": "repo"}}
            return HttpResponse(200, {}, json.dumps(payload).encode())
        if method == "POST" and url.endswith("/v1alpha/sessions"):
            payload = {
                "name": "sessions/new1",
                "state": "QUEUED",
                "sourceContext": {
                    "source": "sources/github/owner/repo",
                    "githubRepoContext": {"startingBranch": "work/w03"},
                },
            }
            return HttpResponse(200, {}, json.dumps(payload).encode())
        if method == "GET" and url.endswith("/v1alpha/sessions/new1"):
            payload = {
                "name": "sessions/new1",
                "state": "QUEUED",
                "sourceContext": {
                    "source": "sources/github/owner/repo",
                    "githubRepoContext": {"startingBranch": "work/w03"},
                },
            }
            return HttpResponse(200, {}, json.dumps(payload).encode())
        return HttpResponse(404, {}, b"{}")


class JulesLifecycleTests(unittest.TestCase):
    def test_terminal_session_is_not_direct_continuation_capable(self) -> None:
        self.assertFalse(terminal_session_continuation_supported("COMPLETED"))
        self.assertFalse(terminal_session_continuation_supported("FAILED"))
        self.assertTrue(terminal_session_continuation_supported("AWAITING_USER_FEEDBACK"))

    def test_create_session_proves_source_branch_and_repository_by_readback(self) -> None:
        transport = CreateSessionTransport()
        client = JulesLifecycleClient("key", transport=transport, endpoint="https://jules.example")
        result = client.create_session(
            prompt="continue writer lineage",
            title="W03 Writer G2",
            source="sources/github/owner/repo",
            starting_branch="work/w03",
            expected_repository="owner/repo",
        )
        self.assertEqual(result["session"], "sessions/new1")
        self.assertEqual(result["repository"], "owner/repo")
        self.assertEqual(result["starting_branch"], "work/w03")
        self.assertTrue(result["authoritative_readback"])
        posts = [item for item in transport.requests if item[0] == "POST"]
        self.assertEqual(len(posts), 1)
        body = json.loads(posts[0][2].decode())
        self.assertEqual(body["sourceContext"]["githubRepoContext"]["startingBranch"], "work/w03")
        self.assertEqual(body["prompt"], "continue writer lineage")


if __name__ == "__main__":
    unittest.main()
