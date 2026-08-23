import unittest
from datetime import datetime, timezone

from ues.lifecycle import (
    AuthorizationDecision,
    Capability,
    CIOutcome,
    FailureClass,
    ForgottenLaneError,
    LifecycleContext,
    LifecycleState,
    NextAction,
    ReviewOutcome,
    SourceBindingStatus,
    StopGate,
    WaitingClass,
    ensure_lifecycle_resolution,
    resolve_next_action,
)
from ues.reconciliation import (
    AuthorizationBinding,
    CIBinding,
    ProviderSourceBinding,
    ReviewBinding,
    WorkstreamBinding,
    canonical_lane_key,
    reconcile_portfolio,
    reconcile_workstream,
)


NOW = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
BASE = "b" * 40
OLD = "a" * 40
NEW = "c" * 40


def provider(
    *,
    repo="hamad933/universal-execution-system",
    session_id="session-1",
    status=SourceBindingStatus.PROVEN_EXPLICIT,
    provider_name="jules",
    source_identity="sources/github/hamad933/universal-execution-system",
    evidence_id="activity-explicit-binding-1",
):
    return ProviderSourceBinding(
        provider=provider_name,
        source_repository=repo,
        source_identity=source_identity,
        session_id=session_id,
        task_id="task-1",
        role="WRITER",
        status=status,
        evidence_id=evidence_id,
    )


def authorization(action, decision=AuthorizationDecision.AUTHORIZED):
    return AuthorizationBinding(
        decision=decision,
        action=action,
        source="project-policy",
        decision_id=f"decision-{action.value}",
    )


def rich_ci(
    *,
    repo="hamad933/universal-execution-system",
    sha=NEW,
    outcome=CIOutcome.PASS,
    run_attempt=2,
):
    return CIBinding(
        source_provider="github",
        source_repository=repo,
        workflow_identity=".github/workflows/core.yml",
        required_check_identity="tests / unit",
        workflow_run_id="32285088809",
        run_attempt=run_attempt,
        job_id="95798373621",
        producer_job="unit",
        artifact_id="4444",
        artifact_name="test-results",
        artifact_digest="sha256:" + "d" * 64,
        candidate_sha=sha,
        classification="REQUIRED_CI_PASS",
        outcome=outcome,
    )


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
        "provider_source": provider(),
    }
    values.update(overrides)
    return WorkstreamBinding(**values)


def authorized_context(state, action, **overrides):
    values = {
        "state": state,
        "authorization_decision": AuthorizationDecision.AUTHORIZED,
        "authorized_action": action,
        "source_binding_status": SourceBindingStatus.PROVEN_EXPLICIT,
    }
    values.update(overrides)
    return LifecycleContext(**values)


class LifecycleTests(unittest.TestCase):
    def test_normal_writer_ci_review_pass(self):
        writer = resolve_next_action(
            authorized_context(
                LifecycleState.WRITER_ACTIVE,
                NextAction.PUBLISH_CANDIDATE,
            )
        )
        self.assertTrue(writer.executable)
        self.assertEqual(writer.action, NextAction.PUBLISH_CANDIDATE)
        self.assertEqual(writer.next_state, LifecycleState.CANDIDATE_PUBLISHED)

        published = resolve_next_action(
            authorized_context(
                LifecycleState.CANDIDATE_PUBLISHED,
                NextAction.RUN_EXACT_HEAD_CI,
            )
        )
        self.assertTrue(published.executable)
        self.assertEqual(published.action, NextAction.RUN_EXACT_HEAD_CI)

        running = resolve_next_action(LifecycleContext(LifecycleState.CI_RUNNING))
        self.assertTrue(running.executable)
        self.assertEqual(running.required_capability, Capability.READ_ONLY)
        self.assertEqual(running.action, NextAction.CLASSIFY_CI)

        classified = resolve_next_action(
            authorized_context(
                LifecycleState.CI_CLASSIFIED,
                NextAction.START_EXACT_SHA_REVIEW,
                ci_outcome=CIOutcome.PASS,
            )
        )
        self.assertTrue(classified.executable)
        self.assertEqual(classified.next_state, LifecycleState.REVIEWER_ACTIVE)
        self.assertEqual(classified.action, NextAction.START_EXACT_SHA_REVIEW)

        reviewing = resolve_next_action(
            LifecycleContext(LifecycleState.REVIEWER_ACTIVE)
        )
        self.assertTrue(reviewing.executable)
        self.assertEqual(reviewing.next_state, LifecycleState.REVIEW_RESULT)

        reviewed = resolve_next_action(
            authorized_context(
                LifecycleState.REVIEW_RESULT,
                NextAction.REQUEST_PARENT_REVIEW,
                review_outcome=ReviewOutcome.PASS,
            )
        )
        self.assertTrue(reviewed.executable)
        self.assertEqual(reviewed.next_state, LifecycleState.PARENT_REVIEW_PENDING)

        parent = resolve_next_action(
            LifecycleContext(LifecycleState.PARENT_REVIEW_PENDING)
        )
        self.assertEqual(parent.stop_gate, StopGate.PARENT_AUTHORITY_REQUIRED)

    def test_findings_same_writer_new_sha_stale_review_and_rereview(self):
        findings = resolve_next_action(
            authorized_context(
                LifecycleState.REVIEW_RESULT,
                NextAction.ROUTE_FINDINGS_TO_SAME_WRITER,
                review_outcome=ReviewOutcome.FINDINGS,
            )
        )
        self.assertTrue(findings.executable)
        self.assertEqual(findings.next_state, LifecycleState.CORRECTION_REQUIRED)

        correction = resolve_next_action(
            authorized_context(
                LifecycleState.CORRECTION_REQUIRED,
                NextAction.CONTINUE_SAME_WRITER,
            )
        )
        self.assertTrue(correction.executable)
        self.assertEqual(
            correction.next_state,
            LifecycleState.SAME_WRITER_CONTINUATION,
        )

        continuation = resolve_next_action(
            LifecycleContext(LifecycleState.SAME_WRITER_CONTINUATION)
        )
        self.assertTrue(continuation.executable)
        self.assertEqual(continuation.next_state, LifecycleState.NEW_SHA)

        new_sha = resolve_next_action(LifecycleContext(LifecycleState.NEW_SHA))
        self.assertTrue(new_sha.executable)
        self.assertEqual(new_sha.action, NextAction.INVALIDATE_PRIOR_REVIEW)

        stale = resolve_next_action(
            authorized_context(
                LifecycleState.PRIOR_REVIEW_STALE,
                NextAction.RUN_EXACT_HEAD_CI,
            )
        )
        self.assertTrue(stale.executable)
        self.assertEqual(stale.next_state, LifecycleState.CI_RUNNING)

        fresh_ci = resolve_next_action(
            authorized_context(
                LifecycleState.CI_CLASSIFIED,
                NextAction.START_RE_REVIEW,
                ci_outcome=CIOutcome.PASS,
                review_stale=True,
            )
        )
        self.assertTrue(fresh_ci.executable)
        self.assertEqual(fresh_ci.next_state, LifecycleState.RE_REVIEW)

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

    def test_waiting_input_is_transition_but_requires_authorization(self):
        waiting = resolve_next_action(
            LifecycleContext(
                LifecycleState.AWAITING_USER_FEEDBACK,
                waiting_class=WaitingClass.POLICY_RESOLVABLE,
                resume_state=LifecycleState.WRITER_ACTIVE,
                source_binding_status=SourceBindingStatus.PROVEN_EXPLICIT,
            )
        )
        self.assertEqual(waiting.action, NextAction.CONTINUE_SAME_SESSION)
        self.assertEqual(waiting.next_state, LifecycleState.WRITER_ACTIVE)
        self.assertFalse(waiting.executable)
        self.assertEqual(
            waiting.stop_gate,
            StopGate.EXTERNAL_AUTHORIZATION_REQUIRED,
        )

    def test_environment_mismatch_without_project_authorization_not_executable(self):
        waiting = resolve_next_action(
            LifecycleContext(
                LifecycleState.AWAITING_USER_FEEDBACK,
                waiting_class=WaitingClass.ENVIRONMENT_MISMATCH,
                resume_state=LifecycleState.WRITER_ACTIVE,
                source_binding_status=SourceBindingStatus.PROVEN_EXPLICIT,
            )
        )
        self.assertEqual(waiting.action, NextAction.CONTINUE_SAME_SESSION)
        self.assertEqual(waiting.required_capability, Capability.MUTATION)
        self.assertFalse(waiting.executable)
        self.assertEqual(
            waiting.stop_gate,
            StopGate.EXTERNAL_AUTHORIZATION_REQUIRED,
        )

    def test_semantic_transition_without_external_authorization_cannot_mutate(self):
        resolution = resolve_next_action(
            LifecycleContext(
                LifecycleState.WRITER_ACTIVE,
                source_binding_status=SourceBindingStatus.PROVEN_EXPLICIT,
            )
        )
        self.assertEqual(resolution.action, NextAction.PUBLISH_CANDIDATE)
        self.assertEqual(resolution.next_state, LifecycleState.CANDIDATE_PUBLISHED)
        self.assertEqual(resolution.required_capability, Capability.MUTATION)
        self.assertIn("external_authorization", resolution.required_evidence)
        self.assertFalse(resolution.executable)
        self.assertEqual(
            resolution.stop_gate,
            StopGate.EXTERNAL_AUTHORIZATION_REQUIRED,
        )

    def test_recoverable_and_paused_transitions_do_not_self_authorize(self):
        failed = resolve_next_action(
            LifecycleContext(
                LifecycleState.FAILED,
                failure_class=FailureClass.RECOVERABLE,
                resume_state=LifecycleState.WRITER_ACTIVE,
                source_binding_status=SourceBindingStatus.PROVEN_EXPLICIT,
            )
        )
        paused = resolve_next_action(
            LifecycleContext(
                LifecycleState.PAUSED,
                resume_state=LifecycleState.WRITER_ACTIVE,
                source_binding_status=SourceBindingStatus.PROVEN_EXPLICIT,
            )
        )
        self.assertFalse(failed.executable)
        self.assertFalse(paused.executable)
        self.assertEqual(
            failed.stop_gate,
            StopGate.EXTERNAL_AUTHORIZATION_REQUIRED,
        )
        self.assertEqual(
            paused.stop_gate,
            StopGate.EXTERNAL_AUTHORIZATION_REQUIRED,
        )

    def test_missing_next_action_is_forgotten_invalid_state(self):
        with self.assertRaisesRegex(ForgottenLaneError, "FORGOTTEN_LANE"):
            ensure_lifecycle_resolution(LifecycleState.WRITER_ACTIVE)


class ReconciliationTests(unittest.TestCase):
    def test_canonical_lane_identity_allows_gs_w01_and_cep_w01(self):
        gs = binding(
            project="GS",
            route="PERSONAL:GS",
            workstream="W01",
            repo="hamad933/goal-system",
            provider_source=provider(
                repo="hamad933/goal-system",
                session_id="gs-session",
                source_identity="sources/github/hamad933/goal-system",
            ),
            lifecycle_state=LifecycleState.SAME_WRITER_CONTINUATION,
            head_sha=NEW,
        )
        cep = binding(
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W01",
            repo="hamad933/Cybersecurity-Education-Platform",
            provider_source=provider(
                repo="hamad933/Cybersecurity-Education-Platform",
                session_id="cep-session",
                source_identity=(
                    "sources/github/hamad933/"
                    "Cybersecurity-Education-Platform"
                ),
            ),
            lifecycle_state=LifecycleState.SAME_WRITER_CONTINUATION,
            head_sha=NEW,
        )
        results = reconcile_portfolio([gs, cep])
        self.assertEqual(
            canonical_lane_key(gs),
            ("GS", "PERSONAL:GS", "W01"),
        )
        self.assertEqual(
            canonical_lane_key(cep),
            ("CEP", "PERSONAL:CEP", "W01"),
        )
        self.assertTrue(all(result.executable for result in results))

    def test_duplicate_same_canonical_lane_fails_closed(self):
        first = binding(
            project="GS",
            route="PERSONAL:GS",
            workstream="W01",
            lifecycle_state=LifecycleState.SAME_WRITER_CONTINUATION,
            head_sha=NEW,
        )
        second = binding(
            project="GS",
            route="PERSONAL:GS",
            workstream="W01",
            provider_source=provider(session_id="session-2"),
            lifecycle_state=LifecycleState.SAME_WRITER_CONTINUATION,
            head_sha=NEW,
        )
        results = reconcile_portfolio([first, second])
        self.assertEqual(
            [r.resolution.stop_gate for r in results],
            [
                StopGate.AMBIGUOUS_LANE_BINDING,
                StopGate.AMBIGUOUS_LANE_BINDING,
            ],
        )
        self.assertFalse(any(r.executable for r in results))

    def test_same_provider_session_bound_to_two_lanes_fails_closed(self):
        first = binding(
            project="UES",
            route="PERSONAL:UES",
            workstream="A",
            provider_source=provider(session_id="shared-session"),
            lifecycle_state=LifecycleState.SAME_WRITER_CONTINUATION,
            head_sha=NEW,
        )
        second = binding(
            project="UES",
            route="PERSONAL:UES",
            workstream="B",
            provider_source=provider(session_id="shared-session"),
            lifecycle_state=LifecycleState.SAME_WRITER_CONTINUATION,
            head_sha=NEW,
        )
        results = reconcile_portfolio([first, second])
        self.assertEqual(
            [r.resolution.stop_gate for r in results],
            [
                StopGate.AMBIGUOUS_PROVIDER_SESSION,
                StopGate.AMBIGUOUS_PROVIDER_SESSION,
            ],
        )

    def test_heuristic_unique_session_remains_unverified(self):
        current = binding(
            provider_source=provider(
                status=SourceBindingStatus.PROPOSED_UNVERIFIED,
                source_identity=None,
                evidence_id=None,
            ),
            authorization=authorization(NextAction.PUBLISH_CANDIDATE),
        )
        result = reconcile_workstream(current)
        self.assertEqual(result.binding.next_action, NextAction.PUBLISH_CANDIDATE.value)
        self.assertFalse(result.executable)
        self.assertEqual(
            result.resolution.stop_gate,
            StopGate.PROVIDER_SOURCE_BINDING_REQUIRED,
        )

    def test_explicit_provider_source_binding_is_accepted(self):
        current = binding(
            authorization=authorization(NextAction.PUBLISH_CANDIDATE)
        )
        result = reconcile_workstream(current)
        self.assertTrue(result.executable)
        self.assertEqual(result.resolution.action, NextAction.PUBLISH_CANDIDATE)
        self.assertIsNone(result.resolution.stop_gate)

    def test_rich_ci_binding_preserves_run_attempt_and_artifact_identity(self):
        ci = rich_ci(run_attempt=3)
        self.assertEqual(ci.source_repository, "hamad933/universal-execution-system")
        self.assertEqual(ci.workflow_identity, ".github/workflows/core.yml")
        self.assertEqual(ci.required_check_identity, "tests / unit")
        self.assertEqual(ci.workflow_run_id, "32285088809")
        self.assertEqual(ci.run_attempt, 3)
        self.assertEqual(ci.job_id, "95798373621")
        self.assertEqual(ci.producer_job, "unit")
        self.assertEqual(ci.artifact_id, "4444")
        self.assertEqual(ci.artifact_name, "test-results")
        self.assertTrue(ci.artifact_digest.startswith("sha256:"))
        self.assertEqual(ci.candidate_sha, NEW)
        self.assertEqual(ci.classification, "REQUIRED_CI_PASS")

    def test_sha_movement_invalidates_prior_exact_sha_review_and_ci(self):
        previous = binding(
            lifecycle_state=LifecycleState.REVIEW_RESULT,
            pr_number=10,
            head_sha=OLD,
            ci=rich_ci(sha=OLD),
            review=ReviewBinding(
                review_id="review-1",
                reviewed_sha=OLD,
                reviewer_lineage="reviewer/session-2",
                source_repository="hamad933/universal-execution-system",
                evidence_classification="EXACT_SHA_REVIEW_PASS",
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
        self.assertTrue(result.prior_ci_invalidated)
        self.assertTrue(result.binding.review.stale)
        self.assertTrue(result.binding.ci.stale)
        self.assertEqual(result.binding.review.reviewed_sha, OLD)
        self.assertEqual(result.binding.ci.candidate_sha, OLD)
        self.assertEqual(result.binding.lifecycle_state, LifecycleState.NEW_SHA)
        self.assertEqual(
            result.resolution.action,
            NextAction.INVALIDATE_PRIOR_REVIEW,
        )

    def test_stale_review_cannot_advance_to_parent(self):
        current = binding(
            lifecycle_state=LifecycleState.REVIEW_RESULT,
            pr_number=10,
            head_sha=NEW,
            review=ReviewBinding(
                review_id="review-1",
                reviewed_sha=OLD,
                reviewer_lineage="reviewer/session-2",
                source_repository="hamad933/universal-execution-system",
                outcome=ReviewOutcome.PASS,
            ),
        )
        result = reconcile_workstream(current)
        self.assertTrue(result.prior_review_invalidated)
        self.assertEqual(
            result.binding.lifecycle_state,
            LifecycleState.PRIOR_REVIEW_STALE,
        )
        self.assertEqual(result.resolution.action, NextAction.RUN_EXACT_HEAD_CI)
        self.assertNotEqual(
            result.binding.next_action,
            NextAction.REQUEST_PARENT_REVIEW.value,
        )

    def test_unknown_or_incomplete_binding_fails_closed(self):
        current = binding(
            lifecycle_state=LifecycleState.CANDIDATE_PUBLISHED,
            pr_number=10,
            head_sha=None,
            authorization=authorization(NextAction.RUN_EXACT_HEAD_CI),
        )
        result = reconcile_workstream(current)
        self.assertFalse(result.executable)
        self.assertEqual(result.resolution.stop_gate, StopGate.INCOMPLETE_BINDING)
        self.assertIn("missing_exact:head_sha", result.issues)

    def test_ci_binding_is_exact_candidate_specific_and_stale_on_mismatch(self):
        current = binding(
            lifecycle_state=LifecycleState.CI_RUNNING,
            pr_number=10,
            head_sha=NEW,
            ci=rich_ci(sha=OLD),
        )
        result = reconcile_workstream(current)
        self.assertFalse(result.executable)
        self.assertTrue(result.prior_ci_invalidated)
        self.assertTrue(result.binding.ci.stale)
        self.assertIn("mismatch:ci.candidate_sha", result.issues)
        self.assertIn("stale:ci", result.issues)

    def test_action_in_flight_reconciles_before_duplicate_action(self):
        current = binding(
            lifecycle_state=LifecycleState.WRITER_ACTIVE,
            action_in_flight="publish-candidate",
            lease_id="lease-1",
            operation_key="op-1",
        )
        result = reconcile_workstream(current)
        self.assertFalse(result.executable)
        self.assertEqual(
            result.resolution.action,
            NextAction.VERIFY_ACTION_IN_FLIGHT,
        )

    def test_exact_sha_binding_rejects_abbreviated_sha(self):
        current = binding(
            head_sha="abc123",
            lifecycle_state=LifecycleState.CANDIDATE_PUBLISHED,
        )
        result = reconcile_workstream(current)
        self.assertFalse(result.executable)
        self.assertIn("invalid:head_sha", result.issues)

    def test_blocked_lane_does_not_freeze_unrelated_lane(self):
        blocked = binding(
            project="UES",
            route="PERSONAL:UES",
            workstream="BLOCKED",
            provider_source=provider(
                session_id="blocked-session",
                status=SourceBindingStatus.PROPOSED_UNVERIFIED,
                source_identity=None,
                evidence_id=None,
            ),
            authorization=authorization(NextAction.PUBLISH_CANDIDATE),
        )
        executable = binding(
            project="UES",
            route="PERSONAL:UES",
            workstream="EXECUTABLE",
            provider_source=provider(session_id="exec-session"),
            lifecycle_state=LifecycleState.SAME_WRITER_CONTINUATION,
            head_sha=NEW,
        )
        results = reconcile_portfolio([blocked, executable])
        by_key = {result.binding.lane_key: result for result in results}
        self.assertFalse(
            by_key[("UES", "PERSONAL:UES", "BLOCKED")].executable
        )
        self.assertTrue(
            by_key[("UES", "PERSONAL:UES", "EXECUTABLE")].executable
        )


if __name__ == "__main__":
    unittest.main()
