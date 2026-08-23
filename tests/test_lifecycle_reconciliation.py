import unittest
from datetime import datetime, timezone

from ues.lifecycle import (
    CIOutcome,
    FailureClass,
    ForgottenLaneError,
    LifecycleContext,
    LifecycleState,
    NextAction,
    ReviewOutcome,
    StopGate,
    WaitingClass,
    ensure_lifecycle_resolution,
    resolve_next_action,
)
from ues.reconciliation import (
    CIBinding,
    ReviewBinding,
    WorkstreamBinding,
    reconcile_portfolio,
    reconcile_workstream,
)


NOW = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
BASE = "b" * 40
OLD = "a" * 40
NEW = "c" * 40


def binding(**overrides):
    values = {
        "project": "UES",
        "route": "PERSONAL:UES",
        "workstream": "UES-AUTO-V2-A",
        "role": "WRITER",
        "repo": "hamad933/universal-execution-system",
        "branch": "work/ues-auto-v2-a-lifecycle",
        "lifecycle_state": LifecycleState.WRITER_ACTIVE,
        "baseline_sha": BASE,
        "base_ref": "automation/portfolio-control-plane-v2",
        "task_budget_class": "NO_NEW_TASK_REQUIRED",
        "last_activity_at": NOW,
        "writer_lineage": "writer/session-1",
        "reviewer_lineage": "reviewer/session-2",
    }
    values.update(overrides)
    return WorkstreamBinding(**values)


class LifecycleTests(unittest.TestCase):
    def test_normal_writer_ci_review_pass(self):
        writer = resolve_next_action(LifecycleContext(LifecycleState.WRITER_ACTIVE))
        self.assertEqual(writer.action, NextAction.PUBLISH_CANDIDATE)
        self.assertEqual(writer.next_state, LifecycleState.CANDIDATE_PUBLISHED)

        published = resolve_next_action(LifecycleContext(LifecycleState.CANDIDATE_PUBLISHED))
        self.assertEqual(published.action, NextAction.RUN_EXACT_HEAD_CI)

        running = resolve_next_action(LifecycleContext(LifecycleState.CI_RUNNING))
        self.assertEqual(running.action, NextAction.CLASSIFY_CI)

        classified = resolve_next_action(
            LifecycleContext(LifecycleState.CI_CLASSIFIED, ci_outcome=CIOutcome.PASS)
        )
        self.assertEqual(classified.next_state, LifecycleState.REVIEWER_ACTIVE)
        self.assertEqual(classified.action, NextAction.START_EXACT_SHA_REVIEW)

        reviewing = resolve_next_action(LifecycleContext(LifecycleState.REVIEWER_ACTIVE))
        self.assertEqual(reviewing.next_state, LifecycleState.REVIEW_RESULT)

        reviewed = resolve_next_action(
            LifecycleContext(LifecycleState.REVIEW_RESULT, review_outcome=ReviewOutcome.PASS)
        )
        self.assertEqual(reviewed.next_state, LifecycleState.PARENT_REVIEW_PENDING)
        self.assertEqual(reviewed.action, NextAction.REQUEST_PARENT_REVIEW)

        parent = resolve_next_action(LifecycleContext(LifecycleState.PARENT_REVIEW_PENDING))
        self.assertEqual(parent.stop_gate, StopGate.PARENT_AUTHORITY_REQUIRED)

    def test_findings_same_writer_new_sha_stale_review_and_rereview(self):
        findings = resolve_next_action(
            LifecycleContext(LifecycleState.REVIEW_RESULT, review_outcome=ReviewOutcome.FINDINGS)
        )
        self.assertEqual(findings.next_state, LifecycleState.CORRECTION_REQUIRED)
        self.assertEqual(findings.action, NextAction.ROUTE_FINDINGS_TO_SAME_WRITER)

        correction = resolve_next_action(LifecycleContext(LifecycleState.CORRECTION_REQUIRED))
        self.assertEqual(correction.next_state, LifecycleState.SAME_WRITER_CONTINUATION)

        continuation = resolve_next_action(LifecycleContext(LifecycleState.SAME_WRITER_CONTINUATION))
        self.assertEqual(continuation.next_state, LifecycleState.NEW_SHA)
        self.assertEqual(continuation.action, NextAction.VERIFY_CANDIDATE_SHA)

        new_sha = resolve_next_action(LifecycleContext(LifecycleState.NEW_SHA))
        self.assertEqual(new_sha.next_state, LifecycleState.PRIOR_REVIEW_STALE)
        self.assertEqual(new_sha.action, NextAction.INVALIDATE_PRIOR_REVIEW)

        stale = resolve_next_action(LifecycleContext(LifecycleState.PRIOR_REVIEW_STALE))
        self.assertEqual(stale.next_state, LifecycleState.CI_RUNNING)
        self.assertEqual(stale.action, NextAction.RUN_EXACT_HEAD_CI)

        fresh_ci = resolve_next_action(
            LifecycleContext(
                LifecycleState.CI_CLASSIFIED,
                ci_outcome=CIOutcome.PASS,
                review_stale=True,
            )
        )
        self.assertEqual(fresh_ci.next_state, LifecycleState.RE_REVIEW)
        self.assertEqual(fresh_ci.action, NextAction.START_RE_REVIEW)

    def test_failed_session_requires_classification(self):
        unknown = resolve_next_action(LifecycleContext(LifecycleState.FAILED))
        self.assertEqual(unknown.stop_gate, StopGate.UNCLASSIFIED_FAILURE)

        terminal = resolve_next_action(
            LifecycleContext(
                LifecycleState.FAILED,
                failure_class=FailureClass.SESSION_CONTINUATION_UNAVAILABLE,
            )
        )
        self.assertEqual(terminal.stop_gate, StopGate.PARENT_REQUIRED_NEW_TASK)

    def test_waiting_input_is_transition(self):
        waiting = resolve_next_action(
            LifecycleContext(
                LifecycleState.AWAITING_USER_FEEDBACK,
                waiting_class=WaitingClass.POLICY_RESOLVABLE,
                resume_state=LifecycleState.WRITER_ACTIVE,
            )
        )
        self.assertEqual(waiting.action, NextAction.CONTINUE_SAME_SESSION)
        self.assertEqual(waiting.next_state, LifecycleState.WRITER_ACTIVE)

    def test_missing_next_action_is_forgotten_invalid_state(self):
        with self.assertRaisesRegex(ForgottenLaneError, "FORGOTTEN_LANE"):
            ensure_lifecycle_resolution(LifecycleState.WRITER_ACTIVE)

    def test_ci_failure_is_explicit_transition(self):
        failed = resolve_next_action(
            LifecycleContext(
                LifecycleState.CI_CLASSIFIED,
                ci_outcome=CIOutcome.FAILURE,
                failure_class=FailureClass.CANDIDATE_DEFECT,
            )
        )
        self.assertEqual(failed.next_state, LifecycleState.CORRECTION_REQUIRED)
        self.assertEqual(failed.action, NextAction.CONTINUE_SAME_WRITER)


class ReconciliationTests(unittest.TestCase):
    def test_sha_movement_invalidates_prior_exact_sha_review(self):
        previous = binding(
            lifecycle_state=LifecycleState.REVIEW_RESULT,
            pr_number=10,
            head_sha=OLD,
            review=ReviewBinding(
                review_id="review-1",
                reviewed_sha=OLD,
                reviewer_lineage="reviewer/session-2",
                outcome=ReviewOutcome.PASS,
            ),
        )
        current = binding(
            lifecycle_state=LifecycleState.SAME_WRITER_CONTINUATION,
            pr_number=10,
            head_sha=NEW,
        )
        result = reconcile_workstream(current, previous)
        self.assertTrue(result.candidate_sha_moved)
        self.assertTrue(result.prior_review_invalidated)
        self.assertTrue(result.binding.review.stale)
        self.assertEqual(result.binding.review.reviewed_sha, OLD)
        self.assertEqual(result.binding.lifecycle_state, LifecycleState.NEW_SHA)
        self.assertEqual(result.resolution.action, NextAction.INVALIDATE_PRIOR_REVIEW)

    def test_stale_review_cannot_advance_to_parent(self):
        current = binding(
            lifecycle_state=LifecycleState.REVIEW_RESULT,
            pr_number=10,
            head_sha=NEW,
            review=ReviewBinding(
                review_id="review-1",
                reviewed_sha=OLD,
                reviewer_lineage="reviewer/session-2",
                outcome=ReviewOutcome.PASS,
            ),
        )
        result = reconcile_workstream(current)
        self.assertTrue(result.prior_review_invalidated)
        self.assertEqual(result.binding.lifecycle_state, LifecycleState.PRIOR_REVIEW_STALE)
        self.assertEqual(result.resolution.action, NextAction.RUN_EXACT_HEAD_CI)
        self.assertNotEqual(result.binding.next_action, NextAction.REQUEST_PARENT_REVIEW.value)

    def test_unknown_or_incomplete_binding_fails_closed(self):
        current = binding(
            lifecycle_state=LifecycleState.CANDIDATE_PUBLISHED,
            pr_number=10,
            head_sha=None,
        )
        result = reconcile_workstream(current)
        self.assertFalse(result.executable)
        self.assertEqual(result.resolution.stop_gate, StopGate.INCOMPLETE_BINDING)
        self.assertIn("missing_exact:head_sha", result.issues)

    def test_independent_blocked_and_executable_lanes(self):
        blocked = binding(
            workstream="BLOCKED",
            lifecycle_state=LifecycleState.CANDIDATE_PUBLISHED,
            pr_number=10,
            head_sha=None,
        )
        executable = binding(
            workstream="EXECUTABLE",
            lifecycle_state=LifecycleState.WRITER_ACTIVE,
        )
        results = reconcile_portfolio([blocked, executable])
        by_id = {result.binding.workstream: result for result in results}
        self.assertFalse(by_id["BLOCKED"].executable)
        self.assertEqual(by_id["BLOCKED"].resolution.stop_gate, StopGate.INCOMPLETE_BINDING)
        self.assertTrue(by_id["EXECUTABLE"].executable)
        self.assertEqual(by_id["EXECUTABLE"].resolution.action, NextAction.PUBLISH_CANDIDATE)

    def test_action_in_flight_reconciles_before_duplicate_action(self):
        current = binding(
            lifecycle_state=LifecycleState.WRITER_ACTIVE,
            action_in_flight="publish-candidate",
            lease_id="lease-1",
            operation_key="op-1",
        )
        result = reconcile_workstream(current)
        self.assertFalse(result.executable)
        self.assertEqual(result.resolution.action, NextAction.VERIFY_ACTION_IN_FLIGHT)
        self.assertEqual(result.binding.next_action, NextAction.VERIFY_ACTION_IN_FLIGHT.value)

    def test_exact_sha_binding_rejects_abbreviated_sha(self):
        current = binding(head_sha="abc123", lifecycle_state=LifecycleState.CANDIDATE_PUBLISHED)
        result = reconcile_workstream(current)
        self.assertFalse(result.executable)
        self.assertIn("invalid:head_sha", result.issues)

    def test_ci_binding_is_exact_candidate_specific(self):
        current = binding(
            lifecycle_state=LifecycleState.CI_RUNNING,
            pr_number=10,
            head_sha=NEW,
            ci=CIBinding(run_id="123", candidate_sha=OLD),
        )
        result = reconcile_workstream(current)
        self.assertFalse(result.executable)
        self.assertIn("mismatch:ci.candidate_sha", result.issues)


if __name__ == "__main__":
    unittest.main()
