from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ues.state_backends.github_refs import GitHubGitDataTransport, GitHubRefTransportError
from ues.state_backends.public_same_repo import OwnerAuthorizedSameRepoGitDataTransport


class OwnerAuthorizedSameRepoReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def transport(self) -> OwnerAuthorizedSameRepoGitDataTransport:
        value = OwnerAuthorizedSameRepoGitDataTransport(
            "hamad933/universal-execution-system",
            "test-token",
            expected_repository="hamad933/universal-execution-system",
        )
        value.read_throttle_attempts = 3
        value.read_throttle_delay_seconds = 0.01
        return value

    def test_snapshot_read_recovers_from_bounded_403(self):
        transport = self.transport()
        expected = {"backend_schema": "ues-github-ref-state-v1"}
        with patch.object(
            GitHubGitDataTransport,
            "read_snapshot",
            side_effect=[
                GitHubRefTransportError("GitHub API request failed (HTTP 403)"),
                expected,
            ],
        ) as read, patch("ues.state_backends.public_same_repo.time.sleep") as sleep:
            self.assertEqual(transport.read_snapshot("a" * 40), expected)
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once()

    def test_ref_read_recovers_from_bounded_429(self):
        transport = self.transport()
        with patch.object(
            GitHubGitDataTransport,
            "get_ref",
            side_effect=[
                GitHubRefTransportError("GitHub API request failed (HTTP 429)"),
                "b" * 40,
            ],
        ) as read, patch("ues.state_backends.public_same_repo.time.sleep"):
            self.assertEqual(transport.get_ref("heads/example"), "b" * 40)
        self.assertEqual(read.call_count, 2)

    def test_non_throttle_read_failure_is_not_retried(self):
        transport = self.transport()
        with patch.object(
            GitHubGitDataTransport,
            "get_ref",
            side_effect=GitHubRefTransportError("GitHub API request failed (HTTP 500)"),
        ) as read, patch("ues.state_backends.public_same_repo.time.sleep") as sleep:
            with self.assertRaises(GitHubRefTransportError):
                transport.get_ref("heads/example")
        self.assertEqual(read.call_count, 1)
        sleep.assert_not_called()

    def test_persistent_403_remains_fail_closed_after_bound(self):
        transport = self.transport()
        with patch.object(
            GitHubGitDataTransport,
            "get_ref",
            side_effect=GitHubRefTransportError("GitHub API request failed (HTTP 403)"),
        ) as read, patch("ues.state_backends.public_same_repo.time.sleep") as sleep:
            with self.assertRaises(GitHubRefTransportError):
                transport.get_ref("heads/example")
        self.assertEqual(read.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_write_methods_are_not_wrapped_in_read_retry(self):
        transport = self.transport()
        with patch.object(
            GitHubGitDataTransport,
            "update_ref",
            side_effect=GitHubRefTransportError("GitHub write outcome uncertain"),
        ) as write, patch("ues.state_backends.public_same_repo.time.sleep") as sleep:
            with self.assertRaises(GitHubRefTransportError):
                transport.update_ref("heads/example", "c" * 40)
        self.assertEqual(write.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
