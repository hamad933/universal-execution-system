from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ues.lifecycle import LifecycleState, NextAction
from ues.project_adapters.gs import (
    PROJECT_ID,
    REPOSITORY,
    ROUTE,
    auto_safe_actions,
    build_binding,
    build_evidence_profile,
    route_reviewer_findings_shadow,
    route_rereview_shadow,
    route_terminal_failure_shadow,
    route_waiting_shadow,
    run_gs_shadow_cycle,
    task_budget_snapshot,
    validate_binding,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
BASE = "a" * 40
HEAD = "b" * 40


class GSShadowAdapterTests(unittest.TestCase):
    def test_identity_and_default_authority_are_fail_closed(self):
        self.assertEqual(PROJECT_ID, "GS")
        self.assertEqual(ROUTE, "GS")
        self.assertEqual(REPOSITORY, "hamad933/GS-2")
        self.assertEqual(auto_safe_actions(), frozenset())

    def test_lifetime_unknown_budget_blocks_new_jules_task(self):
        budget = task_budget_snapshot(current_enumerated_tasks=5)
        self.assertEqual(budget["state"], "UNKNOWN_LIFETIME_CONSUMPTION")
        self.assertIsNone(budget["safe_remaining"])
        self.assertFalse(budget["budget_allows_new_task"])
        self.assertFalse(budget["automatic_new_task_creation"])
        self.assertTrue(budget["fail_closed"])

    def test_evidence_profile_blocks_parent_review_when_assurance_is_unclean(self):
        profile = build_evidence_profile(
            exact_head_ci_proven=True,
            exact_sha_review_proven=True,
            reviewer_contract_clean=False,
        )
        issues = profile.issues_for(NextAction.REQUEST_PARENT_REVIEW)
        self.assertIn("missing_required_evidence:gs_reviewer_contract_clean", issues)
        self.assertNotIn("missing_required_evidence:gs_exact_head_required_ci", issues)

    def test_policy_resolvable_waiting_does_not_gain_external_authority(self):
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

    def test_reviewer_mutation_quarantines_correction_route(self):
        routed = route_reviewer_findings_shadow(
            workstream="HOME",
            writer_session_id="writer-1",
            reviewer_session_id="reviewer-1",
            reviewed_sha=HEAD,
            candidate_sha=HEAD,
            reviewer_role_valid=True,
            reviewer_independent=True,
            reviewer_mutation_detected=True,
            reviewer_mutation_adjudicated=False,
            reviewer_mutation_disqualifying=True,
            writer_binding_proven=True,
            writer_binding_kind="EXPLICIT",
            finding_within_writer_scope=True,
            canonical_operation_active=False,
            canonical_operation_confirmed=False,
            findings=[{"id": "GS-F-001", "root_cause": "ASSURANCE", "paths": ["src/a.ts"]}],
        )
        self.assertEqual(routed["authority"], "DENY")
        self.assertIn("REVIEWER_MUTATION_UNADJUDICATED", routed["failures"])
        self.assertEqual(routed["correction_packet"], [])

    def test_exact_sha_rereview_remains_parent_required_without_action_policy(self):
        routed = route_rereview_shadow(
            workstream="SOLUTIONS",
            writer_session_id="writer-1",
            reviewer_session_id="reviewer-1",
            prior_reviewed_sha=BASE,
            new_candidate_sha=HEAD,
            ci_evidence_sha=HEAD,
            required_ci_proven=True,
            existing_reviewer_available=True,
            existing_reviewer_binding_proven=True,
            existing_reviewer_safe_to_reuse=True,
            new_reviewer_policy_allows=False,
            parent_gate_satisfied=False,
        )
        self.assertTrue(routed["prior_review_stale"])
        self.assertEqual(routed["authority"], "PARENT_REQUIRED")
        self.assertEqual(routed["action"], "ESCALATE_PARENT_RE_REVIEW_DISPATCH")
        self.assertFalse(routed["automatic_new_reviewer_creation"])

    def test_terminal_failed_session_never_auto_creates_replacement(self):
        routed = route_terminal_failure_shadow(same_session_available=False)
        self.assertEqual(routed["authority"], "PARENT_REQUIRED")
        self.assertEqual(routed["action"], "NEW_TASK_RECOMMENDED")
        self.assertFalse(routed["automatic_new_task_creation"])

    def test_shadow_cycle_is_non_mutating_and_parent_gate_is_not_forgotten(self):
        profile = build_evidence_profile(
            exact_head_ci_proven=True,
            exact_sha_review_proven=True,
            reviewer_contract_clean=True,
        )
        binding = build_binding(
            workstream="HOME",
            role="WRITER",
            branch="remediation/example",
            base_ref="integration/example",
            baseline_sha=BASE,
            lifecycle_state=LifecycleState.PARENT_REVIEW_PENDING,
            last_activity_at=NOW,
            head_sha=HEAD,
            scope_identity="gs-home-scope-v1",
            evidence_profile=profile,
        )
        validate_binding(binding)
        result = run_gs_shadow_cycle([binding])
        cycle = result["cycle"]
        self.assertEqual(cycle["activation_mode"], "SHADOW")
        self.assertFalse(cycle["mutation_allowed"])
        self.assertEqual(cycle["external_effects_dispatched"], 0)
        self.assertEqual(cycle["tasks_or_sessions_created"], 0)
        self.assertEqual(cycle["lanes"][0]["stop_gate"], "PARENT_AUTHORITY_REQUIRED")
        self.assertEqual(cycle["watchdog"]["forgotten_lane_count"], 0)

    def test_cross_project_binding_is_rejected_before_shadow_cycle(self):
        binding = build_binding(
            workstream="HOME",
            role="WRITER",
            branch="work/x",
            base_ref="main",
            baseline_sha=BASE,
            lifecycle_state=LifecycleState.PARENT_REVIEW_PENDING,
            last_activity_at=NOW,
        )
        object.__setattr__(binding, "project", "CEP")
        with self.assertRaises(ValueError):
            validate_binding(binding)


if __name__ == "__main__":
    unittest.main()
