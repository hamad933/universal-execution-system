from __future__ import annotations

import unittest
from unittest.mock import patch

from ues.rp_authority_runtime import run


class StaleInitialInventorySnapshotRetryTests(unittest.TestCase):
    def _authority(self) -> dict:
        return {
            "source": "DRIVE_CURRENT_STATE",
            "project": "RP04",
            "route": "RP04",
            "current": True,
            "authority_event_id": "RP04-AUTH-TEST",
            "lineages": {
                "RP04-IPA-S11-001": {
                    "reviewer": {"provider_starting_branch": "main"},
                },
            },
        }

    def test_stale_get_only_inventory_recovers_on_second_snapshot(self):
        unavailable = {
            "result": "STALE_INITIAL_LINEAGE_PROVIDER_READ_UNAVAILABLE",
            "provider_read_error_category": "NETWORK_ERROR",
            "provider_write_attempted": False,
            "reconciled_count": 0,
            "results": [],
            "safe_to_blind_retry": False,
        }
        recovered = {
            "result": "STALE_INITIAL_LINEAGE_RECONCILIATION_COMPLETE",
            "provider_write_attempted": False,
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "reconciled_count": 1,
            "results": [{"decision": "STALE_INITIAL_LINEAGE_AUTHORITATIVELY_RECONCILED"}],
            "safe_to_blind_retry": False,
        }
        live = {"project": "RP04", "external_effects_dispatched": 0, "new_tasks_or_sessions_created": 0}
        with patch.dict("os.environ", {"UES_ALLOW_PUBLIC_SAME_REPO_STATE": "true"}, clear=False), patch(
            "ues.rp_authority_runtime._validated_authority", return_value=self._authority()
        ), patch(
            "ues.rp_authority_runtime.reconcile_project_stale_initial_lineages",
            side_effect=[unavailable, recovered],
        ) as reconcile, patch(
            "ues.rp_authority_runtime.observation_backed_no_effect_eligible", return_value=False
        ), patch("ues.rp_authority_runtime.observed.run", return_value=live):
            result = run("RP04")

        self.assertEqual(reconcile.call_count, 2)
        stale = result["stale_initial_lineage_reconciliation"]
        self.assertEqual(stale["result"], "STALE_INITIAL_LINEAGE_RECONCILIATION_COMPLETE")
        self.assertEqual(stale["provider_inventory_snapshot_attempts"], 2)
        self.assertEqual(stale["provider_inventory_snapshot_attempt_limit"], 2)
        self.assertTrue(stale["provider_inventory_snapshot_retry_get_only"])
        self.assertEqual(stale["reconciled_count"], 1)
        self.assertFalse(stale["provider_write_attempted"])

    def test_stale_get_only_inventory_retry_is_bounded_on_persistent_outage(self):
        unavailable = {
            "result": "STALE_INITIAL_LINEAGE_PROVIDER_READ_UNAVAILABLE",
            "provider_read_error_category": "NETWORK_ERROR",
            "provider_write_attempted": False,
            "reconciled_count": 0,
            "results": [],
            "safe_to_blind_retry": False,
        }
        live = {"project": "RP04", "external_effects_dispatched": 0, "new_tasks_or_sessions_created": 0}
        with patch.dict("os.environ", {"UES_ALLOW_PUBLIC_SAME_REPO_STATE": "true"}, clear=False), patch(
            "ues.rp_authority_runtime._validated_authority", return_value=self._authority()
        ), patch(
            "ues.rp_authority_runtime.reconcile_project_stale_initial_lineages",
            return_value=unavailable,
        ) as reconcile, patch(
            "ues.rp_authority_runtime.observation_backed_no_effect_eligible", return_value=False
        ), patch("ues.rp_authority_runtime.observed.run", return_value=live):
            result = run("RP04")

        self.assertEqual(reconcile.call_count, 2)
        stale = result["stale_initial_lineage_reconciliation"]
        self.assertEqual(stale["result"], "STALE_INITIAL_LINEAGE_PROVIDER_READ_UNAVAILABLE")
        self.assertEqual(stale["provider_inventory_snapshot_attempts"], 2)
        self.assertEqual(stale["provider_inventory_snapshot_attempt_limit"], 2)
        self.assertTrue(stale["provider_inventory_snapshot_retry_get_only"])
        self.assertFalse(stale["provider_write_attempted"])
        self.assertFalse(stale["safe_to_blind_retry"])


if __name__ == "__main__":
    unittest.main()
