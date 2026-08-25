from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from ues.runtime_git_auth import configure_same_repo_git_auth


class RuntimeGitAuthTests(unittest.TestCase):
    def base_env(self) -> dict[str, str]:
        return {
            "GITHUB_ACTIONS": "true",
            "UES_ALLOW_PUBLIC_SAME_REPO_STATE": "true",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_TOKEN": "runtime-token",
        }

    @patch("ues.runtime_git_auth._remote_repository", return_value="owner/repo")
    def test_authorized_same_repo_injects_auth_via_environment_only(self, remote):
        env = self.base_env()
        self.assertTrue(configure_same_repo_git_auth(env))
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        prefix, encoded = env["GIT_CONFIG_VALUE_0"].split(" ", 2)[1:]
        self.assertEqual(prefix, "basic")
        self.assertEqual(base64.b64decode(encoded).decode(), "x-access-token:runtime-token")
        remote.assert_called_once()

    @patch("ues.runtime_git_auth._remote_repository", return_value="other/repo")
    def test_repository_mismatch_fails_closed(self, remote):
        with self.assertRaises(RuntimeError):
            configure_same_repo_git_auth(self.base_env())

    @patch("ues.runtime_git_auth._remote_repository")
    def test_noop_outside_explicit_same_repo_state_mode(self, remote):
        env = self.base_env()
        env["UES_ALLOW_PUBLIC_SAME_REPO_STATE"] = "false"
        self.assertFalse(configure_same_repo_git_auth(env))
        remote.assert_not_called()

    @patch("ues.runtime_git_auth._remote_repository", return_value="owner/repo")
    def test_existing_git_extraheader_is_not_duplicated(self, remote):
        env = self.base_env()
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic existing",
        })
        self.assertTrue(configure_same_repo_git_auth(env))
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "AUTHORIZATION: basic existing")


if __name__ == "__main__":
    unittest.main()
