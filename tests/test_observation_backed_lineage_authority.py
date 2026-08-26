from __future__ import annotations

import unittest
from unittest.mock import patch

from ues.observation_backed_health import observation_backed_no_effect_eligible
from ues.rp_authority_runtime import run


class ObservationBackedLineageAuthorityTests(unittest.TestCase):
    def _adapter(self):
        return {"project": "RP01", "route": "RP01", "repository": "hamad933/Bayt-Style"}

    def test_explicit_zero_effect_lineages_are_observation_backed_eligible(self):
        authority = {
            "lineages": {"RP01-IPA-S07-ORDER-STATUS-001": {"reviewer": {"provider_starting_branch": "evidence"}}},
            "generation_policy": {
                "necessary_generation_authorized": False,
                "generation_effect_authorized": False,
                "authorized_initial_lineages": {},
                "authorized_lineages": {},
            },
            "workflow_dispatches": {},
        }
        with patch("ues.observation_backed_health.legacy._lineage_runtime", return_value={}):
            self.assertTrue(observation_backed_no_effect_eligible(self._adapter(), authority))

    def test_lineages_without_explicit_zero_effect_policy_stay_live_read(self):
        authority = {
            "lineages": {"W01": {"reviewer": {"provider_starting_branch": "main"}}},
            "generation_policy": {},
        }
        with patch("ues.observation_backed_health.legacy._lineage_runtime", return_value={}):
            self.assertFalse(observation_backed_no_effect_eligible(self._adapter(), authority))

    def test_any_authorized_generation_stays_live_read(self):
        authority = {
            "lineages": {"W01": {"reviewer": {"provider_starting_branch": "main"}}},
            "generation_policy": {
                "necessary_generation_authorized": False,
                "generation_effect_authorized": False,
                "authorized_lineages": {"W01:REVIEWER": {"authorized": True}},
            },
        }
        with patch("ues.observation_backed_health.legacy._lineage_runtime", return_value={}):
            self.assertFalse(observation_backed_no_effect_eligible(self._adapter(), authority))

    def test_rp_wrapper_uses_persisted_health_for_explicit_zero_effect_lineages(self):
        authority = {
            "source": "DRIVE_CURRENT_STATE",
            "project": "RP01",
            "route": "RP01",
            "current": True,
            "authority_event_id": "RP01-AUTH-ZERO-EFFECT",
            "lineages": {"RP01-IPA-S07-ORDER-STATUS-001": {"reviewer": {"provider_starting_branch": "evidence"}}},
            "generation_policy": {
                "necessary_generation_authorized": False,
                "generation_effect_authorized": False,
                "authorized_initial_lineages": {},
                "authorized_lineages": {},
            },
        }
        expected = {
            "project": "RP01",
            "result": "OBSERVATION_BACKED_NO_EFFECT_LIFECYCLE_COMPLETE",
            "external_effects_dispatched": 0,
            "new_tasks_or_sessions_created": 0,
            "provider_live_read_performed": False,
        }
        with patch("ues.rp_authority_runtime._validated_authority", return_value=authority), patch(
            "ues.rp_authority_runtime.observation_backed_no_effect_eligible", return_value=True
        ), patch(
            "ues.rp_authority_runtime.run_observation_backed_no_effect_health", return_value=expected
        ) as health, patch("ues.rp_authority_runtime.observed.run") as live:
            result = run("RP01")
        health.assert_called_once()
        live.assert_not_called()
        self.assertFalse(result["provider_live_read_performed"])
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["new_tasks_or_sessions_created"], 0)


if __name__ == "__main__":
    unittest.main()
