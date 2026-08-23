from __future__ import annotations

import json
from pathlib import Path
import unittest


class CEPControlAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter = json.loads(Path("adapters/cep.json").read_text(encoding="utf-8"))

    def test_identity_and_truth_owners_are_exact(self):
        self.assertEqual(self.adapter["project"], "CEP")
        self.assertEqual(self.adapter["route"], "PERSONAL:CEP")
        self.assertEqual(self.adapter["repository"], "hamad933/Cybersecurity-Education-Platform")
        self.assertEqual(
            self.adapter["canonical_lane"]["components"],
            ["project", "route", "workstream"],
        )
        self.assertFalse(self.adapter["canonical_lane"]["allow_bare_workstream_key"])
        self.assertEqual(self.adapter["truth_owners"]["governed_state"], "DRIVE")
        self.assertEqual(self.adapter["truth_owners"]["technical_state"], "GITHUB")

    def test_adapter_is_shadow_only_and_cannot_auto_mutate(self):
        activation = self.adapter["activation"]
        self.assertEqual(activation["default_mode"], "SHADOW")
        self.assertFalse(activation["mutation_allowed"])
        self.assertFalse(activation["runtime_mode_is_authority"])
        self.assertEqual(self.adapter["project_auto_safe_actions"], [])
        self.assertEqual(self.adapter["task_budget"]["new_task_authority"], "PARENT_ONLY")
        self.assertEqual(self.adapter["task_budget"]["unknown_lifetime_capacity"], "DENY")
        self.assertFalse(self.adapter["task_budget"]["automatic_new_task_creation"])

    def test_task_budget_matches_governed_cep_boundary(self):
        budget = self.adapter["task_budget"]
        self.assertEqual(budget["ceiling"], 70)
        self.assertEqual(budget["reserve_target"], 15)
        self.assertEqual(budget["new_task_authority"], "PARENT_ONLY")
        self.assertEqual(budget["unknown_lifetime_capacity"], "DENY")

    def test_provider_effects_require_explicit_source_proof(self):
        binding = self.adapter["actor_binding"]
        self.assertEqual(binding["roles"], ["WRITER", "REVIEWER"])
        self.assertEqual(binding["external_effect_proof_required"], "PROVEN_EXPLICIT")
        self.assertEqual(binding["heuristic_match_status"], "PROPOSED_UNVERIFIED")
        self.assertTrue(binding["source_repository_must_match"])

    def test_current_cep_evidence_identities_are_named_not_sha_pinned(self):
        core = self.adapter["evidence_profiles"]["core_ci"]["requirements"][0]
        browser = self.adapter["evidence_profiles"]["release_browser"]["requirements"][0]
        self.assertEqual(core["workflow"], "Core CI")
        self.assertEqual(browser["workflow"], "Release and Browser Verification")
        self.assertTrue(core["exact_candidate_sha"])
        self.assertTrue(browser["exact_candidate_sha"])
        self.assertTrue(browser["route_profile_required_when_applicable"])
        raw = Path("adapters/cep.json").read_text(encoding="utf-8")
        self.assertNotRegex(raw, r"\b[0-9a-f]{40}\b")
        self.assertNotIn("session_id", raw)
        self.assertNotIn("api_key", raw.lower())

    def test_waiting_rule_is_structured_not_keyword_based(self):
        classifier = self.adapter["waiting_classifier"]
        self.assertFalse(classifier["keyword_shortcuts_allowed"])
        self.assertEqual(classifier["unmatched"], "UNCLASSIFIED")
        self.assertEqual(len(classifier["rules"]), 1)
        rule = classifier["rules"][0]
        self.assertEqual(rule["waiting_class"], "POLICY_RESOLVABLE")
        self.assertEqual(
            rule["match"],
            {
                "provider_state": "AWAITING_USER_FEEDBACK",
                "question_scope": "CONTROLLER_RESOLVABLE",
                "continuation_scope": "SAME_SESSION",
                "scope_expansion": False,
            },
        )
        self.assertNotIn("keyword", rule)
        self.assertNotIn("prompt", rule)
        self.assertNotIn("session", rule)


if __name__ == "__main__":
    unittest.main()
