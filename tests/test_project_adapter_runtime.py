from __future__ import annotations

import copy
import unittest

from ues.project_adapter import (
    ProjectAdapterError,
    build_required_evidence_profile,
    parse_project_adapter,
)


def sample_adapter():
    return {
        "schema_version": "2.0",
        "adapter_kind": "portfolio-project-control",
        "adapter_id": "SAMPLE_SHADOW_V2",
        "project": "SAMPLE",
        "route": "INTERNAL:SAMPLE",
        "repository": "owner/project",
        "activation": {
            "default_mode": "SHADOW",
            "mutation_allowed": False,
            "runtime_mode_is_authority": False,
        },
        "truth_owners": {
            "governed_state": "DRIVE",
            "technical_state": "GITHUB",
            "provider_state": "PROVIDER",
        },
        "canonical_lane": {
            "components": ["project", "route", "workstream"],
            "allow_bare_workstream_key": False,
        },
        "actor_binding": {
            "roles": ["WRITER", "REVIEWER"],
            "external_effect_proof_required": "PROVEN_EXPLICIT",
            "heuristic_match_status": "PROPOSED_UNVERIFIED",
            "source_repository_must_match": True,
        },
        "project_auto_safe_actions": [],
        "task_budget": {
            "new_task_authority": "PARENT_ONLY",
            "unknown_lifetime_capacity": "DENY",
            "automatic_new_task_creation": False,
        },
        "evidence_profiles": {
            "core": {
                "profile_id": "SAMPLE_CORE_V1",
                "requirements": [
                    {
                        "provider": "GITHUB_ACTIONS",
                        "workflow": "Core CI",
                        "job": "validate",
                        "exact_candidate_sha": True,
                        "required": True,
                    }
                ],
            }
        },
        "waiting_classifier": {
            "rules": [],
            "unmatched": "UNCLASSIFIED",
            "keyword_shortcuts_allowed": False,
        },
    }


class ProjectAdapterRuntimeTests(unittest.TestCase):
    def test_valid_adapter_preserves_policy_without_granting_authority(self):
        adapter = parse_project_adapter(sample_adapter())
        self.assertEqual(adapter.project, "SAMPLE")
        self.assertEqual(adapter.route, "INTERNAL:SAMPLE")
        self.assertEqual(adapter.project_auto_safe_actions, ())
        self.assertFalse(adapter.config_grants_mutation_authority)
        self.assertFalse(adapter.mutation_allowed)
        self.assertEqual(adapter.default_mode, "SHADOW")

    def test_runtime_mode_cannot_be_declared_authority(self):
        raw = sample_adapter()
        raw["activation"]["runtime_mode_is_authority"] = True
        with self.assertRaises(ProjectAdapterError):
            parse_project_adapter(raw)

    def test_non_shadow_default_mode_is_rejected(self):
        for mode in ("CANARY", "ACTIVE_AUTO_SAFE"):
            with self.subTest(mode=mode):
                raw = sample_adapter()
                raw["activation"]["default_mode"] = mode
                with self.assertRaises(ProjectAdapterError):
                    parse_project_adapter(raw)

    def test_config_cannot_enable_mutation(self):
        raw = sample_adapter()
        raw["activation"]["mutation_allowed"] = True
        with self.assertRaises(ProjectAdapterError):
            parse_project_adapter(raw)

    def test_bare_workstream_identity_is_rejected(self):
        raw = sample_adapter()
        raw["canonical_lane"]["components"] = ["workstream"]
        with self.assertRaises(ProjectAdapterError):
            parse_project_adapter(raw)

    def test_heuristic_actor_binding_cannot_be_promoted(self):
        raw = sample_adapter()
        raw["actor_binding"]["heuristic_match_status"] = "PROVEN_EXPLICIT"
        with self.assertRaises(ProjectAdapterError):
            parse_project_adapter(raw)

    def test_unknown_task_capacity_must_fail_closed(self):
        raw = sample_adapter()
        raw["task_budget"]["unknown_lifetime_capacity"] = "ALLOW"
        with self.assertRaises(ProjectAdapterError):
            parse_project_adapter(raw)

    def test_keyword_waiting_rules_cannot_be_enabled(self):
        raw = sample_adapter()
        raw["waiting_classifier"]["keyword_shortcuts_allowed"] = True
        with self.assertRaises(ProjectAdapterError):
            parse_project_adapter(raw)

    def test_missing_evidence_is_not_a_pass(self):
        adapter = parse_project_adapter(sample_adapter())
        profile = build_required_evidence_profile(adapter, "core", {})
        requirement = profile.requirements[0]
        self.assertFalse(requirement.proven)
        self.assertFalse(requirement.current)
        self.assertEqual(
            profile.issues_for(None),
            ("missing_required_evidence:GITHUB_ACTIONS:Core CI:validate",),
        )

    def test_exact_observation_can_satisfy_generic_profile(self):
        adapter = parse_project_adapter(sample_adapter())
        profile = build_required_evidence_profile(
            adapter,
            "core",
            {
                "GITHUB_ACTIONS:Core CI:validate": {
                    "proven": True,
                    "current": True,
                    "evidence_id": "run:123/job:456",
                }
            },
        )
        requirement = profile.requirements[0]
        self.assertTrue(requirement.proven)
        self.assertTrue(requirement.current)
        self.assertEqual(requirement.evidence_id, "run:123/job:456")
        self.assertEqual(profile.issues_for(None), ())

    def test_truth_owner_substitution_is_rejected(self):
        raw = copy.deepcopy(sample_adapter())
        raw["truth_owners"]["governed_state"] = "GITHUB"
        with self.assertRaises(ProjectAdapterError):
            parse_project_adapter(raw)


if __name__ == "__main__":
    unittest.main()
