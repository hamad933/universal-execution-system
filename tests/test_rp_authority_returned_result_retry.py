from __future__ import annotations

import unittest
from unittest.mock import patch

from ues.rp_authority_runtime import (
    _PROVIDER_READ_UNAVAILABLE_RESULT,
    run,
)


class ReturnedProviderReadResultRetryTests(unittest.TestCase):
    @staticmethod
    def authority() -> dict:
        return {
            "source": "DRIVE_CURRENT_STATE",
            "project": "RP02",
            "route": "RP02",
            "current": True,
            "authority_event_id": "RP02-AUTH-RETURNED-RESULT-RETRY",
            "lineages": {
                "RP02-IPA-S01-001": {
                    "reviewer": {"provider_starting_branch": "feature/rp02-portfolio-polish-001"}
                }
            },
        }

    @staticmethod
    def outage(*, operation: str = "jules.sessions.list") -> dict:
        return {
            "project": "RP02",
            "result": _PROVIDER_READ_UNAVAILABLE_RESULT,
            "lifecycle_state": "WAITING",
            "provider_read_authoritative": False,
            "provider_read_operation": operation,
            "provider_read_error_category": "NETWORK_ERROR",
            "provider_write_attempted": False,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "safe_to_blind_retry": False,
        }

    def test_structured_zero_effect_outage_retries_then_recovers(self):
        success = {
            "project": "RP02",
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
        }
        with patch("ues.rp_authority_runtime._validated_authority", return_value=self.authority()), patch(
            "ues.rp_authority_runtime.observation_backed_no_effect_eligible", return_value=False
        ), patch(
            "ues.rp_authority_runtime.observed.run", side_effect=[self.outage(), success]
        ) as live:
            result = run("RP02")

        self.assertEqual(live.call_count, 2)
        self.assertEqual(result["provider_inventory_snapshot_attempts"], 2)
        self.assertEqual(result["provider_inventory_snapshot_attempt_limit"], 2)
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)

    def test_structured_zero_effect_outage_exhausts_bounded_attempts(self):
        outage = self.outage()
        with patch("ues.rp_authority_runtime._validated_authority", return_value=self.authority()), patch(
            "ues.rp_authority_runtime.observation_backed_no_effect_eligible", return_value=False
        ), patch("ues.rp_authority_runtime.observed.run", side_effect=[outage, dict(outage)]) as live:
            result = run("RP02")

        self.assertEqual(live.call_count, 2)
        self.assertEqual(result["result"], _PROVIDER_READ_UNAVAILABLE_RESULT)
        self.assertEqual(result["provider_inventory_snapshot_attempts"], 2)
        self.assertFalse(result["provider_write_attempted"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)

    def test_structured_result_for_non_inventory_operation_is_not_retried(self):
        outage = self.outage(operation="jules.activities.list")
        with patch("ues.rp_authority_runtime._validated_authority", return_value=self.authority()), patch(
            "ues.rp_authority_runtime.observation_backed_no_effect_eligible", return_value=False
        ), patch("ues.rp_authority_runtime.observed.run", return_value=outage) as live:
            result = run("RP02")

        live.assert_called_once_with("RP02")
        self.assertEqual(result["provider_inventory_snapshot_attempts"], 1)
        self.assertEqual(result["provider_read_operation"], "jules.activities.list")


if __name__ == "__main__":
    unittest.main()
