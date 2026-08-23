from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ues.lifecycle import LifecycleState, NextAction
from ues.project_adapters.cep import (
    PROJECT_ID,
    REPOSITORY,
    ROUTE,
    TASK_CEILING,
    TASK_RESERVE,
    auto_safe_actions,
    build_binding,
    build_evidence_profile,
    classify_waiting_shadow,
    route_terminal_failure_shadow,
    route_waiting_shadow,
    run_cep_shadow_cycle,
    task_budget_snapshot,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
BASE = "a" * 40
HEAD = "b" * 40


class CEPShadowAdapterTests(unittest.TestCase):
    def test_identity_and_current_default_authority_are_fail_closed(self):
        self.assertEqual(PROJECT_ID, "CEP")
        self.assertEqual(ROUTE, "PERSONAL:CEP")
        self.assertEqual(REPOSITORY, "hamad933/Cybersecurity-Education-Platform")
        self.assertEqual(TASK_CEILING, 70)
        self.assertEqual(TASK_RESERVE, 15)
        self.assertEqual(auto_safe_actions(), frozenset())

    def test_lifetime_unknown_budget_blocks_new_jules_task(self):
        budget = task_budget_snapshot(current_enumerated_tasks=5)
        self.assertEqual(budget["state"], "UNKNOWN_LIFETIME_CONSUMPTION")
        self.assertEqual(budget["reserve"], 15)
        self.assertIsNone(budget["safe_remaining"])
        self.assertFalse(budget["budget_allows_new_task"])
        self.assertFalse(budget["automatic_new_task_creation"])

    def test_keyword_prose_alone_never_classifies_waiting(self):
        result = classify_waiting_shadow(
            {"question": "database architecture migration"},
            provider_state="AWAITING_USER_FEEDBACK",
        )
        self.assertEqual(result["waiting_class"], "UNCLASSIFIED")
        self.assertFalse(result["keyword_shortcut_used"])

    def test_structured_same_session_controller_input_classifies_without_authority(self):
        result = classify_waiting_shadow(
            {
                "question_scope": "CONTROLLER_RESOLVABLE",
                "continuation_scope": "SAME_SESSION",
                "scope_expansion": False,
                "question": "opaque provider prose is not used for the match",
            },
            provider_state="AWAITING_USER_FEEDBACK",
        )
        self.assertEqual(result["waiting_class"], "POLICY_RESOLVABLE")
        self.assertEqual(result["confidence"], "HIGH")
        self.assertFalse(result["keyword_shortcut_used"])
        self.assertEqual(result["authority"], "POLICY_REQUIRED")

    def test_policy_resolvable_waiting_still_escalates_without_project_action_allowlist(self):
        routed = route_waiting_shadow(
            "POLICY_RESOLVABLE",
            exact_state_read=True,
            latest_activity_read=True,
            continuation_binding_proven=True,
            same_session_available=True,
            project_policy_permits=True,
        )
        self.assertEqual(routed["authority"], "PARENT_REQUIRED")
        self.assertEqual(routed["action"], "ESCALATE_PARENT")
        self.assertFalse(routed["automatic_new_task_creation"])

    def test_w05_style_evidence_profile_keeps_route_and_architecture_gates_explicit(self):
        profile = build_evidence_profile(
            core_ci_proven=True,
            release_browser_verification_proven=True,
            exact_sha_review_proven=True,
            route_specific_browser_proven=False,
            architecture_contract_proven=False,
        )
        issues = profile.issues_for(NextAction.REQUEST_PARENT_REVIEW)
        self.assertIn("missing_required_evidence:cep_route_specific_browser_evidence", issues)
        self.assertIn("missing_required_evidence:cep_architecture_contract", issues)
        self.assertNotIn("missing_required_evidence:cep_core_ci", issues)

    def test_terminal_failed_session_does_not_auto_create_replacement(self):
        routed = route_terminal_failure_shadow(same_session_available=False)
        self.assertEqual(routed["authority"], "PARENT_REQUIRED")
        self.assertEqual(routed["action"], "NEW_TASK_RECOMMENDED")
        self.assertFalse(routed["automatic_new_task_creation"])

    def test_shadow_cycle_remains_non_mutating_even_for_parent_gate(self):
        profile = build_evidence_profile(
            core_ci_proven=True,
            release_browser_verification_proven=True,
            exact_sha_review_proven=True,
        )
        binding = build_binding(
            workstream="W01",
            role="WRITER",
            branch="work/cep-w01-example",
            base_ref="build/cep-v1-integration",
            baseline_sha=BASE,
            lifecycle_state=LifecycleState.PARENT_REVIEW_PENDING,
            last_activity_at=NOW,
            head_sha=HEAD,
            scope_identity="cep-w01-scope-v1",
            evidence_profile=profile,
        )
        result = run_cep_shadow_cycle([binding])
        cycle = result["cycle"]
        self.assertEqual(cycle["activation_mode"], "SHADOW")
        self.assertFalse(cycle["mutation_allowed"])
        self.assertEqual(cycle["external_effects_dispatched"], 0)
        self.assertEqual(cycle["tasks_or_sessions_created"], 0)
        self.assertEqual(cycle["lanes"][0]["stop_gate"], "PARENT_AUTHORITY_REQUIRED")
        self.assertEqual(cycle["watchdog"]["forgotten_lane_count"], 0)


if __name__ == "__main__":
    unittest.main()
