from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ues.initial_lineage_runtime import _PROVIDER_READ_UNAVAILABLE_RESULT, run
from ues.providers.base import RateLimitError


class InitialLineageSessionsGetOutageTests(unittest.TestCase):
    def test_sessions_get_rate_limit_during_inventory_hydration_is_zero_effect_receipt(self):
        authority = {
            "source": "DRIVE_CURRENT_STATE",
            "source_id": "authority-source",
            "project": "RP02",
            "route": "RP02",
            "current": True,
            "authority_event_id": "RP02-AUTH-SESSIONS-GET-OUTAGE",
            "generation_policy": {
                "authorized_initial_lineages": {
                    "RP02-IPA-S03:ASSURANCE": {"authorized": True, "task_spec": {}},
                }
            },
        }
        outage = RateLimitError("provider rate limited", operation="jules.sessions.get")
        env = {
            "JULES_API_KEY": "test",
            "GITHUB_TOKEN": "test",
            "UES_AUTHORITY_TRANSPORT_ACTOR": "hamad933",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "ues.initial_lineage_runtime.load_current_authority_json", return_value=authority
        ), patch(
            "ues.initial_lineage_runtime.build_live_state_store", return_value=object()
        ), patch(
            "ues.initial_lineage_runtime.legacy._provider_inventory", side_effect=outage
        ), patch(
            "ues.initial_lineage_runtime.execute_initial_lineage_generation"
        ) as generation:
            result = run("RP02")

        generation.assert_not_called()
        self.assertEqual(result["result"], _PROVIDER_READ_UNAVAILABLE_RESULT)
        self.assertEqual(result["provider_read_operation"], "jules.sessions.get")
        self.assertEqual(result["provider_read_error_category"], "RATE_LIMITED")
        self.assertFalse(result["provider_write_attempted"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)
        self.assertEqual(result["retry_condition"], "FRESH_AUTHORITATIVE_PROVIDER_READ_REQUIRED")
        self.assertFalse(result["safe_to_blind_retry"])


if __name__ == "__main__":
    unittest.main()
