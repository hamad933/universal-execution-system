from __future__ import annotations

import json
from pathlib import Path
import unittest


class GSControlAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter = json.loads(Path("adapters/gs.json").read_text(encoding="utf-8"))

    def test_identity_and_truth_owners_are_exact(self):
        self.assertEqual(self.adapter["project"], "GS")
        self.assertEqual(self.adapter["route"], "GS")
        self.assertEqual(self.adapter["repository"], "hamad933/GS-2")
        self.assertEqual(
            self.adapter["canonical_lane"]["components"],
            ["project", "route", "workstream"],
        )
        self.assertFalse(self.adapter["canonical_lane"]["allow_bare_workstream_key"])
        self.assertEqual(self.adapter["truth_owners"]["governed_state"], "DRIVE")
        self.assertEqual(self.adapter["truth_owners"]["technical_state"], "GITHUB")

    def test_adapter_remains_shadow_and_auto_generation_is_fail_closed_pending_runtime_binding(self):
        activation = self.adapter["activation"]
        self.assertEqual(activation["default_mode"], "SHADOW")
        self.assertFalse(activation["mutation_allowed"])
        self.assertFalse(activation["runtime_mode_is_authority"])
        self.assertEqual(self.adapter["project_auto_safe_actions"], [])
        runtime = self.adapter["lineage_runtime"]
        self.assertFalse(runtime["auto_create_next_generation"])
        self.assertFalse(runtime["new_session_budget_safe"])
        self.assertIn("BLOCKED_PENDING", runtime["generation_activation_status"])
        self.assertTrue(runtime["unbound_never_implies_replacement"])
        self.assertTrue(runtime["replacement_requires_proven_terminal_or_context_exhausted"])

    def test_task_budget_matches_g93_owner_policy_without_activating_runtime_creation(self):
        budget = self.adapter["task_budget"]
        self.assertEqual(budget["ceiling"], 40)
        self.assertIsNone(budget["reserve_target"])
        self.assertEqual(
            budget["reserve_status"],
            "NOT_DEFINED_BY_CURRENT_GS_AUTHORITY",
        )
        self.assertEqual(budget["new_task_authority"], "PARENT_ONLY")
        self.assertEqual(
            budget["owner_new_task_policy"],
            "OWNER_AUTHORIZED_NECESSITY_BASED_NEW_GENERATION",
        )
        self.assertTrue(budget["necessity_based_new_generation_authorized"])
        self.assertEqual(
            budget["unknown_lifetime_capacity"],
            "ALLOW_UNLESS_DIRECT_CEILING_REACHED",
        )
        self.assertFalse(budget["automatic_new_task_creation"])
        self.assertTrue(budget["runtime_budget_preflight_required"])
        self.assertFalse(budget["unknown_lifetime_alone_is_stop_gate"])
        self.assertNotIn("proven_used_floor", budget)
        self.assertNotIn("direct_ceiling_reached", budget)

    def test_provider_effects_require_explicit_source_proof(self):
        binding = self.adapter["actor_binding"]
        self.assertEqual(binding["roles"], ["WRITER", "REVIEWER"])
        self.assertEqual(binding["external_effect_proof_required"], "PROVEN_EXPLICIT")
        self.assertEqual(binding["heuristic_match_status"], "PROPOSED_UNVERIFIED")
        self.assertTrue(binding["source_repository_must_match"])

    def test_current_gs_evidence_identities_are_named_not_sha_pinned(self):
        core = self.adapter["evidence_profiles"]["core_ci"]["requirements"][0]
        visual = self.adapter["evidence_profiles"]["visual_assurance"]["requirements"][0]
        self.assertEqual((core["workflow"], core["job"]), ("CI", "validate"))
        self.assertEqual(visual["workflow"], "Visual Evidence")
        self.assertTrue(core["exact_candidate_sha"])
        self.assertTrue(visual["exact_candidate_sha"])
        raw = Path("adapters/gs.json").read_text(encoding="utf-8")
        self.assertNotRegex(raw, r"\b[0-9a-f]{40}\b")
        self.assertNotIn("session_id", raw)
        self.assertNotIn("api_key", raw.lower())

    def test_waiting_unknown_fails_closed(self):
        classifier = self.adapter["waiting_classifier"]
        self.assertEqual(classifier["rules"], [])
        self.assertEqual(classifier["unmatched"], "UNCLASSIFIED")
        self.assertFalse(classifier["keyword_shortcuts_allowed"])

    def test_speed_policy_does_not_remove_safety_prohibitions(self):
        prohibitions = set(self.adapter["prohibitions"])
        for item in {
            "MERGE",
            "RELEASE",
            "DEPLOY",
            "PRODUCT_PUBLICATION",
            "FORCE_PUSH",
            "TEST_WEAKENING",
            "GUESSED_SESSION_OWNERSHIP",
            "BLIND_WRITE_RETRY",
            "UNGUARDED_AUTOMATIC_NEW_JULES_TASK",
        }:
            self.assertIn(item, prohibitions)
        self.assertEqual(
            self.adapter["watchdog_policy"]["thresholds"]["reuse_critical_path_drag_seconds"],
            600,
        )


if __name__ == "__main__":
    unittest.main()
