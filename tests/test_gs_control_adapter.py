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
        self.assertEqual(self.adapter["canonical_lane"]["components"], ["project", "route", "workstream"])
        self.assertFalse(self.adapter["canonical_lane"]["allow_bare_workstream_key"])
        self.assertEqual(self.adapter["truth_owners"]["governed_state"], "DRIVE")
        self.assertEqual(self.adapter["truth_owners"]["technical_state"], "GITHUB")

    def test_adapter_remains_shadow_and_dynamic_lineages_require_current_authority(self):
        activation = self.adapter["activation"]
        self.assertEqual(activation["default_mode"], "SHADOW")
        self.assertFalse(activation["mutation_allowed"])
        self.assertFalse(activation["runtime_mode_is_authority"])
        runtime = self.adapter["lineage_runtime"]
        self.assertTrue(runtime["current_authority_required_for_new_generation"])
        self.assertTrue(runtime["dynamic_governed_lineages_allowed"])
        self.assertTrue(runtime["unbound_never_implies_replacement"])
        self.assertTrue(runtime["replacement_requires_governed_cause"])

    def test_task_budget_does_not_commit_current_gs_ceiling_or_owner_decision(self):
        budget = self.adapter["task_budget"]
        self.assertEqual(budget["new_task_authority"], "PARENT_ONLY")
        self.assertEqual(budget["unknown_lifetime_capacity"], "DENY")
        self.assertFalse(budget["automatic_new_task_creation"])
        self.assertTrue(budget["current_ceiling_must_be_resolved_at_runtime"])
        self.assertTrue(budget["runtime_budget_preflight_required"])
        self.assertNotIn("ceiling", budget)
        self.assertNotIn("reserve_target", budget)
        self.assertNotIn("necessity_based_new_generation_authorized", budget)
        self.assertNotIn("owner_new_task_policy", budget)

    def test_lineage_topology_does_not_require_future_session_fingerprints(self):
        runtime = self.adapter["lineage_runtime"]
        for config in runtime["workstreams"].values():
            for role in ("writer", "reviewer"):
                policy = config.get(role)
                if not isinstance(policy, dict):
                    continue
                self.assertEqual(policy.get("known_session_fingerprints"), [])
                self.assertNotIn("pr_number", policy)
                self.assertNotIn("starting_branch", policy)
                self.assertNotIn("provider_starting_branch", policy)
        self.assertNotIn("authority_event_id", runtime)

    def test_provider_effects_require_explicit_source_proof(self):
        binding = self.adapter["actor_binding"]
        self.assertEqual(binding["roles"], ["WRITER", "REVIEWER"])
        self.assertEqual(binding["external_effect_proof_required"], "PROVEN_EXPLICIT")
        self.assertEqual(binding["heuristic_match_status"], "PROPOSED_UNVERIFIED")
        self.assertTrue(binding["source_repository_must_match"])

    def test_evidence_contracts_are_named_not_sha_pinned(self):
        core = self.adapter["evidence_profiles"]["core_ci"]["requirements"][0]
        visual = self.adapter["evidence_profiles"]["visual_assurance"]["requirements"][0]
        self.assertEqual((core["workflow"], core["job"]), ("CI", "validate"))
        self.assertEqual(visual["workflow"], "Visual Evidence")
        self.assertTrue(core["exact_candidate_sha"])
        self.assertTrue(visual["exact_candidate_sha"])

    def test_no_mutable_live_truth_is_committed(self):
        raw = Path("adapters/gs.json").read_text(encoding="utf-8")
        self.assertNotRegex(raw, r"\b[0-9a-f]{40}\b")
        self.assertNotRegex(raw, r"\b[0-9a-f]{64}\b")
        self.assertNotIn("session_id", raw)
        self.assertNotIn('"current_sha":', raw)
        self.assertNotIn('"current_candidate_sha":', raw)
        self.assertNotIn("authority_event_id", raw)
        self.assertNotIn("generation_activation_status", raw)
        self.assertNotIn("api_key", raw.lower())

    def test_waiting_unknown_fails_closed(self):
        classifier = self.adapter["waiting_classifier"]
        self.assertEqual(classifier["rules"], [])
        self.assertEqual(classifier["unmatched"], "UNCLASSIFIED")
        self.assertFalse(classifier["keyword_shortcuts_allowed"])

    def test_speed_policy_does_not_remove_safety_prohibitions(self):
        prohibitions = set(self.adapter["prohibitions"])
        for item in {
            "MERGE", "RELEASE", "DEPLOY", "PRODUCT_PUBLICATION", "FORCE_PUSH",
            "TEST_WEAKENING", "GUESSED_SESSION_OWNERSHIP", "BLIND_WRITE_RETRY",
            "UNGUARDED_AUTOMATIC_NEW_JULES_TASK",
        }:
            self.assertIn(item, prohibitions)
        self.assertEqual(self.adapter["watchdog_policy"]["thresholds"]["reuse_critical_path_drag_seconds"], 600)


if __name__ == "__main__":
    unittest.main()
