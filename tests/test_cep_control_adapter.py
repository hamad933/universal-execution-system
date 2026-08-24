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
        self.assertEqual(self.adapter["canonical_lane"]["components"], ["project", "route", "workstream"])
        self.assertFalse(self.adapter["canonical_lane"]["allow_bare_workstream_key"])
        self.assertEqual(self.adapter["truth_owners"]["governed_state"], "DRIVE")
        self.assertEqual(self.adapter["truth_owners"]["technical_state"], "GITHUB")

    def test_adapter_remains_shadow_and_current_authority_is_external(self):
        activation = self.adapter["activation"]
        self.assertEqual(activation["default_mode"], "SHADOW")
        self.assertFalse(activation["mutation_allowed"])
        self.assertFalse(activation["runtime_mode_is_authority"])
        self.assertEqual(self.adapter["project_auto_safe_actions"], [])
        transport = self.adapter["authority_transport"]
        self.assertEqual(transport["canonical_source"], "DRIVE_CURRENT_STATE")
        self.assertFalse(transport["transport_is_truth_owner"])
        self.assertTrue(transport["bounded_expiry_required"])

    def test_task_budget_contains_only_fail_closed_stable_defaults(self):
        budget = self.adapter["task_budget"]
        self.assertEqual(budget["new_task_authority"], "PARENT_ONLY")
        self.assertEqual(budget["unknown_lifetime_capacity"], "DENY")
        self.assertFalse(budget["automatic_new_task_creation"])
        self.assertTrue(budget["current_ceiling_must_be_resolved_at_runtime"])
        self.assertNotIn("ceiling", budget)
        self.assertNotIn("reserve_target", budget)
        self.assertNotIn("used", budget)

    def test_lineage_topology_contains_no_live_provider_or_pr_binding(self):
        runtime = self.adapter["lineage_runtime"]
        self.assertTrue(runtime["current_authority_required_for_new_generation"])
        self.assertTrue(runtime["reuse_same_session_first"])
        self.assertIn("CEP-AUTO-001", runtime["workstreams"])
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

    def test_evidence_and_dispatch_contracts_are_stable_and_bounded(self):
        core = self.adapter["evidence_profiles"]["core_ci"]["requirements"][0]
        browser = self.adapter["evidence_profiles"]["release_browser"]["requirements"][0]
        self.assertEqual(core["workflow"], "Core CI")
        self.assertEqual(browser["workflow"], "Release and Browser Verification")
        self.assertTrue(browser["route_profile_required_when_applicable"])
        dispatch = self.adapter["workflow_dispatch_policy"]
        self.assertFalse(dispatch["allow_arbitrary_workflow"])
        release = dispatch["workflows"]["release_browser"]
        self.assertEqual(release["workflow"], ".github/workflows/release-verification.yml")
        self.assertIn("W05", release["allowed_inputs"]["route_profiles"])

    def test_no_mutable_live_truth_is_committed(self):
        raw = Path("adapters/cep.json").read_text(encoding="utf-8")
        self.assertNotRegex(raw, r"\b[0-9a-f]{40}\b")
        self.assertNotRegex(raw, r"\b[0-9a-f]{64}\b")
        self.assertNotIn("session_id", raw)
        self.assertNotIn('"current_sha":', raw)
        self.assertNotIn('"current_candidate_sha":', raw)
        self.assertNotIn("authority_event_id", raw)
        self.assertNotIn("waiting_continuations\": [\n      {", raw)
        self.assertNotIn("api_key", raw.lower())

    def test_waiting_rule_is_structured_not_keyword_based(self):
        classifier = self.adapter["waiting_classifier"]
        self.assertFalse(classifier["keyword_shortcuts_allowed"])
        self.assertEqual(classifier["unmatched"], "UNCLASSIFIED")
        rule = classifier["rules"][0]
        self.assertEqual(rule["waiting_class"], "POLICY_RESOLVABLE")
        self.assertNotIn("keyword", rule)
        self.assertNotIn("prompt", rule)
        self.assertNotIn("session", rule)


if __name__ == "__main__":
    unittest.main()
