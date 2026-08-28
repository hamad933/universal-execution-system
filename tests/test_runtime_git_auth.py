from __future__ import annotations

import base64
import subprocess
import unittest
from unittest.mock import Mock, patch

from ues.runtime_git_auth import _local_git_extraheader_present, configure_same_repo_git_auth


class RuntimeGitAuthTests(unittest.TestCase):
    def base_env(self) -> dict[str, str]:
        return {
            "GITHUB_ACTIONS": "true",
            "UES_ALLOW_PUBLIC_SAME_REPO_STATE": "true",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_TOKEN": "runtime-token",
        }

    @patch("ues.runtime_git_auth._local_git_extraheader_present", return_value=False)
    @patch("ues.runtime_git_auth._remote_repository", return_value="owner/repo")
    def test_authorized_same_repo_injects_auth_via_environment_only(self, remote, local_header):
        env = self.base_env()
        self.assertTrue(configure_same_repo_git_auth(env))
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        prefix, encoded = env["GIT_CONFIG_VALUE_0"].split(" ", 2)[1:]
        self.assertEqual(prefix, "basic")
        self.assertEqual(base64.b64decode(encoded).decode(), "x-access-token:runtime-token")
        remote.assert_called_once()
        local_header.assert_called_once()

    @patch("ues.runtime_git_auth._local_git_extraheader_present")
    @patch("ues.runtime_git_auth._remote_repository", return_value="other/repo")
    def test_repository_mismatch_fails_closed(self, remote, local_header):
        with self.assertRaises(RuntimeError):
            configure_same_repo_git_auth(self.base_env())
        local_header.assert_not_called()

    @patch("ues.runtime_git_auth._remote_repository")
    def test_noop_outside_explicit_same_repo_state_mode(self, remote):
        env = self.base_env()
        env["UES_ALLOW_PUBLIC_SAME_REPO_STATE"] = "false"
        self.assertFalse(configure_same_repo_git_auth(env))
        remote.assert_not_called()

    @patch("ues.runtime_git_auth._local_git_extraheader_present")
    @patch("ues.runtime_git_auth._remote_repository", return_value="owner/repo")
    def test_existing_environment_extraheader_is_not_duplicated(self, remote, local_header):
        env = self.base_env()
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic existing",
        })
        self.assertTrue(configure_same_repo_git_auth(env))
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "AUTHORIZATION: basic existing")
        local_header.assert_not_called()

    @patch("ues.runtime_git_auth._local_git_extraheader_present", return_value=True)
    @patch("ues.runtime_git_auth._remote_repository", return_value="owner/repo")
    def test_checkout_persisted_local_extraheader_is_reused_without_environment_duplicate(
        self, remote, local_header
    ):
        env = self.base_env()
        self.assertTrue(configure_same_repo_git_auth(env))
        self.assertNotIn("GIT_CONFIG_COUNT", env)
        self.assertFalse(any(key.startswith("GIT_CONFIG_KEY_") for key in env))
        self.assertFalse(any(key.startswith("GIT_CONFIG_VALUE_") for key in env))
        local_header.assert_called_once()

    @patch("ues.runtime_git_auth.subprocess.run")
    def test_local_header_probe_never_reads_header_value(self, run):
        run.return_value = Mock(returncode=0)
        self.assertTrue(_local_git_extraheader_present())
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            [
                "git",
                "config",
                "--local",
                "--name-only",
                "--get-regexp",
                r"^http\.https://github\.com/\.extraheader$",
            ],
        )
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
