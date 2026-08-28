from __future__ import annotations

import unittest
from unittest.mock import patch

from ues.providers.base import NetworkError
from ues.rp_authority_runtime import _PROVIDER_READ_UNAVAILABLE_RESULT, run


class RPSessionHydrationRetryTests(unittest.TestCase):
    def authority(self) -> dict[str, object]:
        return {
            "source": "DRIVE_CURRENT_STATE",
            "project": "RP02",
            "route": "RP02",
            "current": True,
            "authority_event_id": "RP02-AUTH-HYDRATION",
            "lineages": {"W01": {"reviewer": {"provider_starting_branch": "main"}}},
        }

    def test_transient_sessions_get_failure_recovers_on_second_snapshot(self):
        outage = NetworkError("provider network request failed", operation="jules.sessions.get")
        success = {
            "project": "RP02",
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
        }
        with patch("ues.rp_authority_runtime._validated_authority", return_value=self.authority()), patch(
            "ues.rp_authority_runtime.observed.run", side_effect=[outage, success]
        ) as live:
            result = run("RP02")

        self.assertEqual(live.call_count, 2)
        self.assertEqual(result["provider_inventory_snapshot_attempts"], 2)
        self.assertTrue(result["provider_inventory_snapshot_retry_pre_effect_only"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)

    def test_persistent_sessions_get_failure_fails_closed_after_bound(self):
        outage = NetworkError("provider network request failed", operation="jules.sessions.get")
        with patch("ues.rp_authority_runtime._validated_authority", return_value=self.authority()), patch(
            "ues.rp_authority_runtime.observed.run", side_effect=outage
        ) as live:
            result = run("RP02")

        self.assertEqual(live.call_count, 2)
        self.assertEqual(result["result"], _PROVIDER_READ_UNAVAILABLE_RESULT)
        self.assertEqual(result["provider_read_operation"], "jules.sessions.get")
        self.assertFalse(result["provider_write_attempted"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)
        self.assertFalse(result["safe_to_blind_retry"])
        self.assertEqual(result["provider_inventory_snapshot_attempts"], 2)

    def test_non_inventory_provider_failure_is_never_outer_retried(self):
        outage = NetworkError("provider network request failed", operation="jules.activities.list")
        with patch("ues.rp_authority_runtime._validated_authority", return_value=self.authority()), patch(
            "ues.rp_authority_runtime.observed.run", side_effect=outage
        ) as live:
            with self.assertRaises(NetworkError):
                run("RP02")

        live.assert_called_once_with("RP02")


if __name__ == "__main__":
    unittest.main()
