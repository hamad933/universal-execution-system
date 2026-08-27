from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ues import rp_readonly_runtime as runtime


class RPReadOnlyObserverRetryTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("UES_RP_PROVIDER_OBSERVER_ATTEMPTS", None)

    @staticmethod
    def failure(category: str = "NETWORK_ERROR", **extra: object) -> dict[str, object]:
        return {
            "result": "JULES_PROVIDER_OBSERVATION_FAILED",
            "error_category": category,
            "provider_mutation_performed": False,
            "new_tasks_or_sessions_created": 0,
            **extra,
        }

    @staticmethod
    def success() -> dict[str, object]:
        return {
            "result": "JULES_PROVIDER_OBSERVATION_COMPLETE",
            "provider_mutation_performed": False,
            "new_tasks_or_sessions_created": 0,
        }

    def test_transient_pre_snapshot_failure_retries_once_then_recovers(self) -> None:
        with patch.object(runtime.provider_runtime, "observe", side_effect=[self.failure(), self.success()]) as observe:
            result = runtime.observe_provider()
        self.assertEqual(observe.call_count, 2)
        self.assertEqual(result["result"], "JULES_PROVIDER_OBSERVATION_COMPLETE")
        self.assertEqual(result["observer_attempt_count"], 2)
        self.assertEqual(result["observer_attempt_limit"], 2)
        self.assertTrue(result["observer_recovered_after_retry"])
        self.assertFalse(result["provider_mutation_performed"])
        self.assertFalse(result["safe_to_blind_retry"])

    def test_repeated_transient_failure_exhausts_at_bounded_limit(self) -> None:
        os.environ["UES_RP_PROVIDER_OBSERVER_ATTEMPTS"] = "99"
        with patch.object(runtime.provider_runtime, "observe", side_effect=[self.failure(), self.failure(), self.failure()]) as observe:
            result = runtime.observe_provider()
        self.assertEqual(observe.call_count, 3)
        self.assertEqual(result["result"], "JULES_PROVIDER_OBSERVATION_FAILED")
        self.assertEqual(result["observer_attempt_count"], 3)
        self.assertEqual(result["observer_attempt_limit"], 3)
        self.assertFalse(result["observer_recovered_after_retry"])
        self.assertFalse(result["safe_to_blind_retry"])

    def test_non_transient_failure_is_not_retried(self) -> None:
        with patch.object(runtime.provider_runtime, "observe", return_value=self.failure("AUTHENTICATION_ERROR")) as observe:
            result = runtime.observe_provider()
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(result["observer_attempt_count"], 1)
        self.assertFalse(result["observer_recovered_after_retry"])

    def test_post_snapshot_or_persistence_failure_is_not_retried(self) -> None:
        failure = self.failure(
            "NETWORK_ERROR",
            provider_read_complete=True,
            state_persistence_complete=False,
            sanitized_recovery_snapshot={"provider_read_complete": True},
        )
        with patch.object(runtime.provider_runtime, "observe", return_value=failure) as observe:
            result = runtime.observe_provider()
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(result["observer_attempt_count"], 1)
        self.assertFalse(result["observer_recovered_after_retry"])

    def test_invalid_attempt_env_uses_default_two(self) -> None:
        os.environ["UES_RP_PROVIDER_OBSERVER_ATTEMPTS"] = "invalid"
        with patch.object(runtime.provider_runtime, "observe", side_effect=[self.failure("SERVER_ERROR"), self.success()]) as observe:
            result = runtime.observe_provider()
        self.assertEqual(observe.call_count, 2)
        self.assertEqual(result["observer_attempt_limit"], 2)


if __name__ == "__main__":
    unittest.main()
