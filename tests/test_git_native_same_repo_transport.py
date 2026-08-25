from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest.mock import patch

from ues.state_backends.github_refs import GitHubRefConflict, GitHubRefWriteUncertain
from ues.state_backends.public_same_repo import OwnerAuthorizedSameRepoGitDataTransport


class Result:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class GitNativeSameRepoTransportTests(unittest.TestCase):
    def transport(self):
        return OwnerAuthorizedSameRepoGitDataTransport(
            "hamad933/universal-execution-system",
            "runtime-only-secret",
            expected_repository="hamad933/universal-execution-system",
        )

    def actions_env(self):
        return patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False)

    def test_actions_identity_uses_git_remote_without_rest_metadata(self):
        transport = self.transport()
        with self.actions_env(), patch(
            "ues.state_backends.public_same_repo.subprocess.run",
            return_value=Result("https://github.com/hamad933/universal-execution-system.git\n"),
        ) as run, patch.object(transport, "_request_json") as rest:
            transport.assert_private_repository()
        self.assertTrue(transport._storage_policy_verified)
        self.assertEqual(transport.storage_visibility, "OWNER_AUTHORIZED_SAME_REPO")
        self.assertEqual(run.call_args.args[0][:4], ["git", "remote", "get-url", "origin"])
        rest.assert_not_called()

    def test_actions_identity_mismatch_fails_closed(self):
        transport = self.transport()
        with self.actions_env(), patch(
            "ues.state_backends.public_same_repo.subprocess.run",
            return_value=Result("https://github.com/hamad933/other.git\n"),
        ):
            with self.assertRaises(Exception):
                transport.assert_private_repository()

    def test_ref_read_uses_ls_remote_and_never_rest(self):
        transport = self.transport()
        sha = "a" * 40
        output = f"{sha}\trefs/heads/ues-runtime/v2/lane/example\n"
        with self.actions_env(), patch(
            "ues.state_backends.public_same_repo.subprocess.run",
            return_value=Result(output),
        ), patch.object(transport, "_request_json") as rest:
            observed = transport.get_ref("heads/ues-runtime/v2/lane/example")
        self.assertEqual(observed, sha)
        rest.assert_not_called()

    def test_snapshot_read_fetches_and_reads_state_json(self):
        transport = self.transport()
        sha = "b" * 40
        snapshot = {"backend_schema": "ues-github-ref-state-v1", "version": 1}
        calls = [Result(""), Result(json.dumps(snapshot))]
        with self.actions_env(), patch(
            "ues.state_backends.public_same_repo.subprocess.run",
            side_effect=calls,
        ) as run:
            observed = transport.read_snapshot(sha)
        self.assertEqual(observed, snapshot)
        self.assertEqual(run.call_count, 2)
        self.assertIn("fetch", run.call_args_list[0].args[0])
        self.assertIn("show", run.call_args_list[1].args[0])

    def test_normal_push_rejection_is_cas_conflict_and_never_retried(self):
        transport = self.transport()
        with self.actions_env(), patch(
            "ues.state_backends.public_same_repo.subprocess.run",
            return_value=Result(stderr="! [rejected] state -> state (fetch first)", returncode=1),
        ) as run:
            with self.assertRaises(GitHubRefConflict):
                transport.update_ref("heads/state", "c" * 40)
        self.assertEqual(run.call_count, 1)

    def test_ambiguous_push_failure_is_uncertain_and_never_retried(self):
        transport = self.transport()
        with self.actions_env(), patch(
            "ues.state_backends.public_same_repo.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git push", timeout=15),
        ) as run:
            with self.assertRaises(GitHubRefWriteUncertain):
                transport.update_ref("heads/state", "d" * 40)
        self.assertEqual(run.call_count, 1)

    def test_outside_actions_preserves_existing_rest_path(self):
        transport = self.transport()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}, clear=False), patch.object(
            transport,
            "_request_json",
            return_value={
                "full_name": "hamad933/universal-execution-system",
                "private": False,
            },
        ) as rest:
            transport.assert_private_repository()
        self.assertEqual(transport.storage_visibility, "PUBLIC")
        rest.assert_called_once()


if __name__ == "__main__":
    unittest.main()
