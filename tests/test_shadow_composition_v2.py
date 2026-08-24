from __future__ import annotations

from pathlib import Path
import unittest

from ues.project_adapter import build_required_evidence_profile, load_project_adapter


class ShadowCompositionV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gs = load_project_adapter(Path("adapters/gs.json"))
        cls.cep = load_project_adapter(Path("adapters/cep.json"))

    def test_project_configs_remain_shadow_only_and_defer_mutable_budget_policy(self):
        for adapter in (self.gs, self.cep):
            with self.subTest(project=adapter.project):
                self.assertEqual(adapter.default_mode, "SHADOW")
                self.assertFalse(adapter.mutation_allowed)
                self.assertFalse(adapter.config_grants_mutation_authority)
                self.assertEqual(adapter.project_auto_safe_actions, ())
                self.assertFalse(adapter.automatic_new_task_creation)
                self.assertEqual(adapter.unknown_lifetime_capacity, "DENY")
                budget = adapter.raw["task_budget"]
                self.assertTrue(budget["current_ceiling_must_be_resolved_at_runtime"])
                self.assertNotIn("ceiling", budget)
                self.assertNotIn("reserve_target", budget)

    def test_project_lane_identity_is_distinct(self):
        self.assertNotEqual(
            (self.gs.project, self.gs.route, "W01"),
            (self.cep.project, self.cep.route, "W01"),
        )

    def test_missing_current_evidence_does_not_pass(self):
        for adapter in (self.gs, self.cep):
            with self.subTest(project=adapter.project):
                profile = build_required_evidence_profile(adapter, "core_ci", {})
                issues = profile.issues_for(None)
                self.assertTrue(issues)
                self.assertTrue(
                    any(item.startswith("missing_required_evidence:") for item in issues)
                )


if __name__ == "__main__":
    unittest.main()
