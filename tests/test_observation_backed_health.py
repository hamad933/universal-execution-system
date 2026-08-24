from __future__ import annotations

import unittest

from ues.observation_backed_health import observation_backed_no_effect_eligible
from ues.rp_readonly_runtime import _load_rp_adapter
from ues.lifecycle_runtime_observed import _promote_effect_counts


class ObservationBackedHealthTests(unittest.TestCase):
    def test_empty_rp_topology_is_eligible(self):
        adapter = _load_rp_adapter("RP01")
        authority = {
            "lineages": {},
            "generation_policy": {
                "authorized_initial_lineages": {},
                "authorized_lineages": {},
            },
            "workflow_dispatches": {},
        }
        self.assertTrue(observation_backed_no_effect_eligible(adapter, authority))

    def test_any_effect_capable_topology_forces_live_runtime(self):
        adapter = _load_rp_adapter("RP01")
        cases = [
            {"lineages": {"W01": {"writer": {}}}},
            {"generation_policy": {"authorized_initial_lineages": {"W01:WRITER": {"authorized": True}}}},
            {"generation_policy": {"authorized_lineages": {"W01:WRITER": {"authorized": True}}}},
            {"workflow_dispatches": {"W01": {"authorized": True}}},
        ]
        for authority in cases:
            with self.subTest(authority=authority):
                self.assertFalse(observation_backed_no_effect_eligible(adapter, authority))

    def test_effect_counters_are_promoted_without_guessing(self):
        result = {
            "summary": {
                "external_effects_dispatched": 0,
                "new_tasks_or_sessions_created": 0,
            }
        }
        promoted = _promote_effect_counts(result)
        self.assertEqual(promoted["external_effects_dispatched"], 0)
        self.assertEqual(promoted["new_tasks_or_sessions_created"], 0)

        missing = _promote_effect_counts({"summary": {}})
        self.assertNotIn("external_effects_dispatched", missing)
        self.assertNotIn("new_tasks_or_sessions_created", missing)


if __name__ == "__main__":
    unittest.main()
