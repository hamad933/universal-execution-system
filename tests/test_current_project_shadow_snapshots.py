from __future__ import annotations

from pathlib import Path
import unittest

from ues.policy_resolution import resolve_execution_policy
from ues.project_adapter import load_project_adapter
from ues.project_shadow import evaluate_project_shadow
from ues.routing import route_reviewer_to_writer


class CurrentProjectShadowSnapshotsTests(unittest.TestCase):
    """Regression of authority separation across stable adapters and current policy.

    Current governed facts are supplied explicitly to policy resolution. They are
    not copied into the committed adapter and never make SHADOW mutation-capable.
    """

    @classmethod
    def setUpClass(cls):
        cls.gs = load_project_adapter(Path("adapters/gs.json"))
        cls.cep = load_project_adapter(Path("adapters/cep.json"))

    @staticmethod
    def gs_current_authority() -> dict:
        return {
            "current": True,
            "authority_event_id": "GS-CURRENT-TEST",
            "task_budget": {
                "ceiling": 40,
                "unknown_lifetime_capacity": "ALLOW_UNLESS_DIRECT_CEILING_REACHED",
            },
            "generation_policy": {"necessary_generation_authorized": False},
        }

    def test_gs_current_policy_allows_unknown_history_capacity_without_adapter_mutation_authority(self):
        shadow = evaluate_project_shadow(
            self.gs,
            evidence_observations={"core_ci": {}},
        )
        resolved = resolve_execution_policy(
            adapter=self.gs.raw,
            governed_authority=self.gs_current_authority(),
            provider_observation={
                "lifetime_consumption_known": False,
                "current_enumerated_tasks": 5,
            },
        ).to_dict()

        self.assertEqual(shadow["activation_mode"], "SHADOW")
        self.assertFalse(shadow["mutation_allowed"])
        self.assertFalse(shadow["evidence"]["core_ci"]["complete"])
        self.assertIn(
            "missing_required_evidence:GITHUB_ACTIONS:CI:validate",
            shadow["evidence"]["core_ci"]["issues"],
        )
        self.assertEqual(
            resolved["budget"]["state"],
            "OWNER_POLICY_CAPACITY_AVAILABLE_WITH_UNKNOWN_LIFETIME",
        )
        self.assertTrue(resolved["budget"]["budget_allows_new_task"])
        self.assertFalse(resolved["generation_allowed"])
        self.assertEqual(resolved["ceiling"], 40)
        self.assertEqual(
            resolved["provenance"]["ceiling"],
            "governed_authority.task_budget.ceiling",
        )
        self.assertFalse(resolved["provenance"]["adapter_mutable_snapshot_is_authority"])
        self.assertEqual(shadow["external_effects_dispatched"], 0)
        self.assertEqual(shadow["tasks_or_sessions_created"], 0)

    def test_gs_direct_enumeration_at_current_ceiling_blocks_even_when_unknown_history_policy_allows(self):
        resolved = resolve_execution_policy(
            adapter=self.gs.raw,
            governed_authority=self.gs_current_authority(),
            provider_observation={
                "lifetime_consumption_known": False,
                "current_enumerated_tasks": 40,
            },
        ).to_dict()
        self.assertEqual(
            resolved["budget"]["state"],
            "DIRECT_CEILING_OR_RESERVE_BOUNDARY_REACHED",
        )
        self.assertFalse(resolved["budget"]["budget_allows_new_task"])
        self.assertFalse(resolved["generation_allowed"])

    def test_gs_unproven_writer_binding_cannot_receive_correction(self):
        routed = route_reviewer_to_writer(
            project=self.gs.project,
            route=self.gs.route,
            workstream_id="CURRENT-REVIEW-LANE",
            writer_session_id=None,
            reviewer_session_id="sanitized-reviewer",
            reviewed_sha="a" * 40,
            candidate_sha="a" * 40,
            reviewer_role_valid=True,
            reviewer_independent=True,
            reviewer_mutation_detected=False,
            reviewer_mutation_adjudicated=False,
            reviewer_mutation_disqualifying=False,
            writer_binding_proven=False,
            writer_binding_kind="UNPROVEN",
            finding_within_writer_scope=True,
            canonical_operation_active=False,
            canonical_operation_confirmed=False,
            findings=[
                {
                    "id": "SANITIZED-EXACT-SHA-FINDING",
                    "root_cause": "BINDING_PROOF_REQUIRED",
                    "summary": "sanitized",
                    "paths": ["sanitized/path"],
                }
            ],
            project_auto_safe_actions=self.gs.project_auto_safe_actions,
        )
        self.assertEqual(routed["authority"], "DENY")
        self.assertIn("WRITER_BINDING_UNPROVEN", routed["failures"])
        self.assertFalse(routed["reuse_existing_writer"])
        self.assertFalse(routed["automatic_new_task_creation"])

    def test_cep_structured_same_session_question_is_classified_but_not_auto_sent(self):
        result = evaluate_project_shadow(
            self.cep,
            evidence_observations={
                "core_ci": {
                    "GITHUB_ACTIONS:Core CI": {
                        "proven": True,
                        "current": True,
                        "evidence_id": "sanitized-current-core-ci",
                    }
                }
            },
            waiting_observations=[
                {
                    "provider_state": "AWAITING_USER_FEEDBACK",
                    "exact_state_read": True,
                    "latest_activity_read": True,
                    "continuation_binding_proven": True,
                    "same_session_available": True,
                    "project_policy_permits": True,
                    "activity": {
                        "question_scope": "CONTROLLER_RESOLVABLE",
                        "continuation_scope": "SAME_SESSION",
                        "scope_expansion": False,
                    },
                }
            ],
        )
        waiting = result["waiting"][0]
        self.assertEqual(waiting["classification"]["waiting_class"], "POLICY_RESOLVABLE")
        self.assertFalse(waiting["classification"]["keyword_shortcut_used"])
        self.assertEqual(waiting["route"]["authority"], "PARENT_REQUIRED")
        self.assertEqual(waiting["route"]["action"], "ESCALATE_PARENT")
        self.assertEqual(result["external_effects_dispatched"], 0)
        self.assertEqual(result["tasks_or_sessions_created"], 0)
        self.assertFalse(result["new_task_gate"]["allowed"])

    def test_unmatched_cep_waiting_evidence_remains_unclassified(self):
        result = evaluate_project_shadow(
            self.cep,
            waiting_observations=[
                {
                    "provider_state": "AWAITING_USER_FEEDBACK",
                    "exact_state_read": True,
                    "latest_activity_read": True,
                    "continuation_binding_proven": True,
                    "activity": {"question_scope": "UNKNOWN"},
                }
            ],
        )
        waiting = result["waiting"][0]
        self.assertEqual(waiting["classification"]["waiting_class"], "UNCLASSIFIED")
        self.assertEqual(waiting["route"]["authority"], "DENY")
        self.assertEqual(waiting["route"]["action"], "STOP")
        self.assertEqual(result["external_effects_dispatched"], 0)


if __name__ == "__main__":
    unittest.main()
