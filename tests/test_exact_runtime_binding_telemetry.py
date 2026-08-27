from __future__ import annotations

import unittest
from unittest.mock import patch

from ues.lifecycle_runtime_observed import runtime_binding_from_env


class ExactRuntimeBindingTelemetryTests(unittest.TestCase):
    def base_env(self) -> dict[str, str]:
        return {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "hamad933/universal-execution-system",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_REF": "refs/pull/58/merge",
            "GITHUB_EVENT_NAME": "pull_request",
        }

    def test_explicit_exact_runtime_sha_wins_over_trigger_sha(self) -> None:
        env = self.base_env()
        env["UES_EXACT_RUNTIME_SHA"] = "b" * 40
        binding = runtime_binding_from_env(env)

        self.assertEqual(binding["status"], "BOUND")
        self.assertEqual(binding["sha"], "b" * 40)
        self.assertEqual(binding["trigger_sha"], "a" * 40)
        self.assertEqual(binding["source"], "UES_EXACT_RUNTIME_ENV")
        self.assertTrue(binding["telemetry_grants_no_authority"])

    def test_matching_trigger_and_exact_runtime_remain_bound(self) -> None:
        env = self.base_env()
        env["GITHUB_SHA"] = "c" * 40
        env["UES_EXACT_RUNTIME_SHA"] = "c" * 40
        binding = runtime_binding_from_env(env)

        self.assertEqual(binding["status"], "BOUND")
        self.assertEqual(binding["sha"], "c" * 40)
        self.assertEqual(binding["trigger_sha"], "c" * 40)
        self.assertFalse(binding.get("runtime_binding_grants_authority", False))
        self.assertTrue(binding["telemetry_grants_no_authority"])

    def test_malformed_explicit_exact_runtime_fails_closed_without_trigger_fallback(self) -> None:
        env = self.base_env()
        env["UES_EXACT_RUNTIME_SHA"] = "not-a-sha"
        binding = runtime_binding_from_env(env)

        self.assertEqual(binding["status"], "UNBOUND")
        self.assertNotIn("sha", binding)
        self.assertEqual(binding["trigger_sha"], "a" * 40)
        self.assertEqual(binding["source"], "UES_EXACT_RUNTIME_ENV")
        self.assertTrue(binding["telemetry_grants_no_authority"])

    def test_real_runtime_prefers_exact_checked_out_git_head_when_no_explicit_input(self) -> None:
        env = self.base_env()
        with patch.dict("os.environ", env, clear=True), patch(
            "ues.lifecycle_runtime_observed._checked_out_runtime_sha", return_value="d" * 40
        ):
            binding = runtime_binding_from_env()

        self.assertEqual(binding["status"], "BOUND")
        self.assertEqual(binding["sha"], "d" * 40)
        self.assertEqual(binding["trigger_sha"], "a" * 40)
        self.assertEqual(binding["source"], "CHECKED_OUT_GIT_HEAD")
        self.assertTrue(binding["telemetry_grants_no_authority"])

    def test_custom_environment_without_exact_input_stays_deterministic(self) -> None:
        env = self.base_env()
        binding = runtime_binding_from_env(env)

        self.assertEqual(binding["status"], "BOUND")
        self.assertEqual(binding["sha"], "a" * 40)
        self.assertEqual(binding["trigger_sha"], "a" * 40)
        self.assertEqual(binding["source"], "GITHUB_ACTIONS_TRIGGER_ENV")
        self.assertTrue(binding["telemetry_grants_no_authority"])


if __name__ == "__main__":
    unittest.main()
