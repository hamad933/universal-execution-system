from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest

from ues.control_loop import run_shadow_cycle
from ues.identity import canonical_lane_id
from ues.lifecycle import LifecycleState
from ues.project_adapter import build_required_evidence_profile, load_project_adapter
from ues.reconciliation import WorkstreamBinding
from ues.routing import classify_waiting_activity
from ues.state_backends import GitHubRefStateStore
from ues.task_budget import evaluate_task_budget

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
BASE = "a" * 40
HEAD = "b" * 40


class ShadowCompleteCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gs = load_project_adapter(Path("adapters/gs.json"))
        cls.cep = load_project_adapter(Path("adapters/cep.json"))

    def test_real_project_configs_load_through_generic_runtime(self):
        self.assertEqual((self.gs.project, self.gs.route), ("GS", "GS"))
        self.assertEqual((self.cep.project, self.cep.route), ("CEP", "PERSONAL:CEP"))
        for adapter in (self.gs, self.cep):
            self.assertEqual(adapter.default_mode, "SHADOW")
            self.assertFalse(adapter.mutation_allowed)
            self.assertFalse(adapter.runtime_mode_is_authority)
            self.assertFalse(adapter.config_grants_mutation_authority)
            self.assertEqual(adapter.project_auto_safe_actions, ())
            self.assertFalse(adapter.automatic_new_task_creation)

    def test_same_w01_in_gs_and_cep_cannot_collide(self):
        gs_lane = canonical_lane_id(self.gs.project, self.gs.route, "W01")
        cep_lane = canonical_lane_id(self.cep.project, self.cep.route, "W01")
        self.assertNotEqual(gs_lane, cep_lane)

    def test_governed_task_budget_boundaries_are_project_specific_and_runtime_safe(self):
        gs_budget = self.gs.raw["task_budget"]
        cep_budget = self.cep.raw["task_budget"]
        self.assertEqual(gs_budget["ceiling"], 40)
        self.assertIsNone(gs_budget["reserve_target"])
        self.assertEqual(
            gs_budget["unknown_lifetime_capacity"],
            "ALLOW_UNLESS_DIRECT_CEILING_REACHED",
        )
        self.assertTrue(gs_budget["necessity_based_new_generation_authorized"])
        self.assertFalse(gs_budget["automatic_new_task_creation"])
        self.assertTrue(gs_budget["runtime_budget_preflight_required"])
        self.assertEqual(cep_budget["ceiling"], 70)
        self.assertEqual(cep_budget["reserve_target"], 15)
        self.assertEqual(cep_budget["unknown_lifetime_capacity"], "DENY")

        gs_unknown = evaluate_task_budget(
            project="GS",
            ceiling=gs_budget["ceiling"],
            reserve=0,
            lifetime_consumption_known=False,
            proven_lifetime_used=None,
            current_enumerated_tasks=5,
            unknown_lifetime_policy=gs_budget["unknown_lifetime_capacity"],
        )
        self.assertEqual(
            gs_unknown["state"],
            "OWNER_POLICY_CAPACITY_AVAILABLE_WITH_UNKNOWN_LIFETIME",
        )
        self.assertTrue(gs_unknown["budget_allows_new_task"])
        self.assertFalse(gs_unknown["fail_closed"])

        gs_ceiling = evaluate_task_budget(
            project="GS",
            ceiling=gs_budget["ceiling"],
            reserve=0,
            lifetime_consumption_known=False,
            proven_lifetime_used=None,
            current_enumerated_tasks=40,
            unknown_lifetime_policy=gs_budget["unknown_lifetime_capacity"],
        )
        self.assertEqual(
            gs_ceiling["state"],
            "DIRECT_CEILING_OR_RESERVE_BOUNDARY_REACHED",
        )
        self.assertFalse(gs_ceiling["budget_allows_new_task"])
        self.assertTrue(gs_ceiling["fail_closed"])

        cep_unknown = evaluate_task_budget(
            project="CEP",
            ceiling=cep_budget["ceiling"],
            reserve=cep_budget["reserve_target"],
            lifetime_consumption_known=False,
            proven_lifetime_used=None,
            current_enumerated_tasks=5,
        )
        self.assertEqual(cep_unknown["state"], "UNKNOWN_LIFETIME_CONSUMPTION")
        self.assertFalse(cep_unknown["budget_allows_new_task"])
        self.assertTrue(cep_unknown["fail_closed"])

    def test_cep_structured_waiting_rule_matches_without_keyword_shortcut(self):
        matched = classify_waiting_activity(
            {
                "question_scope": "CONTROLLER_RESOLVABLE",
                "continuation_scope": "SAME_SESSION",
                "scope_expansion": False,
                "question": "database architecture migration words are irrelevant",
            },
            provider_state="AWAITING_USER_FEEDBACK",
            classifier_rules=self.cep.waiting_classifier_rules,
        )
        self.assertEqual(matched["waiting_class"], "POLICY_RESOLVABLE")
        self.assertFalse(matched["keyword_shortcut_used"])
        self.assertEqual(matched["authority"], "POLICY_REQUIRED")

        keyword_only = classify_waiting_activity(
            {"question": "database architecture migration"},
            provider_state="AWAITING_USER_FEEDBACK",
            classifier_rules=self.cep.waiting_classifier_rules,
        )
        self.assertEqual(keyword_only["waiting_class"], "UNCLASSIFIED")
        self.assertFalse(keyword_only["keyword_shortcut_used"])

    def test_missing_real_project_evidence_is_not_pass(self):
        gs_profile = build_required_evidence_profile(self.gs, "core_ci", {})
        cep_profile = build_required_evidence_profile(self.cep, "release_browser", {})
        self.assertTrue(gs_profile.issues_for(None))
        self.assertTrue(cep_profile.issues_for(None))
        self.assertFalse(gs_profile.requirements[0].proven)
        self.assertFalse(cep_profile.requirements[0].current)

    def test_exact_observations_can_satisfy_profiles_without_changing_authority(self):
        gs_spec = self.gs.evidence_profile_spec("core_ci").requirements[0]
        cep_spec = self.cep.evidence_profile_spec("release_browser").requirements[0]
        gs_profile = build_required_evidence_profile(
            self.gs,
            "core_ci",
            {gs_spec.requirement_id: {"proven": True, "current": True, "evidence_id": "gs-ci"}},
        )
        cep_profile = build_required_evidence_profile(
            self.cep,
            "release_browser",
            {cep_spec.requirement_id: {"proven": True, "current": True, "evidence_id": "cep-browser"}},
        )
        self.assertEqual(gs_profile.issues_for(None), ())
        self.assertEqual(cep_profile.issues_for(None), ())
        self.assertFalse(self.gs.config_grants_mutation_authority)
        self.assertFalse(self.cep.config_grants_mutation_authority)

    def test_state_backend_is_packaged_with_project_runtime(self):
        self.assertEqual(GitHubRefStateStore.__name__, "GitHubRefStateStore")

    def test_two_project_shadow_cycle_is_non_mutating_and_not_forgotten(self):
        bindings = [
            WorkstreamBinding(
                project=self.gs.project,
                route=self.gs.route,
                workstream="W01",
                role="WRITER",
                repo=self.gs.repository,
                branch="shadow/gs-w01",
                lifecycle_state=LifecycleState.PARENT_REVIEW_PENDING,
                baseline_sha=BASE,
                base_ref="integration/gs",
                task_budget_class="PARENT_ONLY",
                last_activity_at=NOW,
                head_sha=HEAD,
            ),
            WorkstreamBinding(
                project=self.cep.project,
                route=self.cep.route,
                workstream="W01",
                role="WRITER",
                repo=self.cep.repository,
                branch="shadow/cep-w01",
                lifecycle_state=LifecycleState.PARENT_REVIEW_PENDING,
                baseline_sha=BASE,
                base_ref="build/cep-v1-integration",
                task_budget_class="PARENT_ONLY",
                last_activity_at=NOW,
                head_sha=HEAD,
            ),
        ]
        cycle = run_shadow_cycle(bindings)
        self.assertEqual(cycle["activation_mode"], "SHADOW")
        self.assertFalse(cycle["mutation_allowed"])
        self.assertEqual(cycle["external_effects_dispatched"], 0)
        self.assertEqual(cycle["tasks_or_sessions_created"], 0)
        self.assertEqual(cycle["lane_count"], 2)
        self.assertEqual(cycle["watchdog"]["forgotten_lanes"], [])
        self.assertEqual(
            {lane["stop_gate"] for lane in cycle["lanes"]},
            {"PARENT_AUTHORITY_REQUIRED"},
        )


if __name__ == "__main__":
    unittest.main()
