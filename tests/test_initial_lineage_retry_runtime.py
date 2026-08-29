from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ues import initial_lineage_retry_runtime as retry


class InitialLineageRetryRuntimeTests(unittest.TestCase):
    @staticmethod
    def unavailable() -> dict:
        return {
            "result": retry.runtime._PROVIDER_READ_UNAVAILABLE_RESULT,
            "provider_write_attempted": False,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "safe_to_blind_retry": False,
        }

    def test_transient_zero_effect_read_failure_recovers_on_second_attempt(self):
        success = {"result": "INITIAL_LINEAGE_RUNTIME_COMPLETE", "external_effects_dispatched": 1}
        with patch.dict(os.environ, {retry.ATTEMPTS_ENV: "3"}, clear=False), patch.object(
            retry.runtime, "run", side_effect=[self.unavailable(), success]
        ) as run:
            result = retry.run("RP03")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["result"], "INITIAL_LINEAGE_RUNTIME_COMPLETE")
        self.assertEqual(result["provider_snapshot_attempts"], 2)
        self.assertEqual(result["provider_snapshot_retries_used"], 1)
        self.assertEqual(result["provider_snapshot_attempt_limit"], 3)

    def test_retry_exhaustion_is_capped_and_preserves_fail_closed_result(self):
        with patch.dict(os.environ, {retry.ATTEMPTS_ENV: "9"}, clear=False), patch.object(
            retry.runtime, "run", side_effect=[self.unavailable(), self.unavailable(), self.unavailable()]
        ) as run:
            result = retry.run("RP03")
        self.assertEqual(run.call_count, retry.MAX_ATTEMPTS)
        self.assertEqual(result["result"], retry.runtime._PROVIDER_READ_UNAVAILABLE_RESULT)
        self.assertEqual(result["provider_snapshot_attempts"], 3)
        self.assertEqual(result["provider_snapshot_retries_used"], 2)
        self.assertFalse(result["safe_to_blind_retry"])

    def test_possible_write_state_is_never_retried(self):
        unsafe = self.unavailable()
        unsafe["provider_write_attempted"] = True
        with patch.dict(os.environ, {retry.ATTEMPTS_ENV: "3"}, clear=False), patch.object(
            retry.runtime, "run", return_value=unsafe
        ) as run:
            result = retry.run("RP03")
        run.assert_called_once_with("RP03")
        self.assertEqual(result["provider_snapshot_attempts"], 1)

    def test_non_read_failure_is_never_retried(self):
        other = {"result": "INITIAL_LINEAGE_RUNTIME_BLOCKED", "external_effects_dispatched": 0}
        with patch.dict(os.environ, {retry.ATTEMPTS_ENV: "3"}, clear=False), patch.object(
            retry.runtime, "run", return_value=other
        ) as run:
            result = retry.run("RP03")
        run.assert_called_once_with("RP03")
        self.assertEqual(result["result"], "INITIAL_LINEAGE_RUNTIME_BLOCKED")

    def test_attempt_limit_defaults_and_clamps(self):
        self.assertEqual(retry._attempt_limit(""), retry.DEFAULT_ATTEMPTS)
        self.assertEqual(retry._attempt_limit("bad"), retry.DEFAULT_ATTEMPTS)
        self.assertEqual(retry._attempt_limit("0"), 1)
        self.assertEqual(retry._attempt_limit("99"), retry.MAX_ATTEMPTS)

    def test_cli_keeps_exit_75_after_bounded_exhaustion(self):
        result = self.unavailable()
        result.update(
            provider_snapshot_attempts=3,
            provider_snapshot_retries_used=2,
            provider_snapshot_attempt_limit=3,
        )
        with patch.object(retry, "run", return_value=result):
            rc = retry.main(["RP03"])
        self.assertEqual(rc, retry.runtime._PROVIDER_READ_UNAVAILABLE_EXIT)


if __name__ == "__main__":
    unittest.main()
