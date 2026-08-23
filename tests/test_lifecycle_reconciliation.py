import unittest
from datetime import datetime, timezone

from ues.lifecycle import (
    ActionCapability,
    CIOutcome,
    FailureClass,
    LifecycleContext,
    LifecycleState,
    NextAction,
    ReviewOutcome,
    SourceBindingStatus,
    StopGate,
    WaitingClass,
    resolve_next_action,
)
from ues.reconciliation import (
    ActorBinding,
    CIBinding,
    EvidenceRequirement,
    RequiredEvidenceProfile,
    ReviewBinding,
    WorkstreamBinding,
    canonical_lane_key,
    reconcile_portfolio,
    reconcile_workstream,
    resolve_actor_binding,
)


NOW = datetime(2026, 8, 23, 20, 30, tzinfo=timezone.utc)
BASE = "b" * 40
OLD = "a" * 40
NEW = "c" * 40
REPO = "hamad933/universal-execution-system"


def actor(
    role="WRITER",
    *,
    session_id=None,
    repo=REPO,
    status=SourceBindingStatus.PROVEN_EXPLICIT,
):
    role = role.upper()
    session_id = session_id or f"{role.lower()}-session"
    return ActorBinding(
        role=role,
        provider="jules",
        session_id=session_id,
        task_id=f"{role.lower()}-task",
        lineage=f"{role.lower()}-lineage",
        source_repository=repo,
        source_identity=f"sources/{repo}",
        proof_status=status,
        evidence_id=f"binding-{role.lower()}-{session_id}",
    )


def review(sha=NEW, outcome=ReviewOutcome.PASS):
    return ReviewBinding(
        review_id="review-1",
        reviewed_sha=sha,
        reviewer_lineage="reviewer-lineage",
        source_repository=REPO,
        evidence_classification="EXACT_SHA_REVIEW",
        outcome=outcome,
    )


def ci(sha=NEW, outcome=CIOutcome.PASS):
    return CIBinding(
        source_provider="github",
        source_repository=REPO,
        workflow_identity=".github/workflows/validate.yml",
        required_check_identity="Validate Universal Core / core",
        workflow_run_id="12345",
        run_attempt=1,
        job_id="98765",
        producer_job="core",
        candidate_sha=sha,
        classification="REQUIRED_CI_PASS",
        outcome=outcome,
    )


def profile(*requirements, profile_id="profile-r2"):
    return RequiredEvidenceProfile(profile_id, tuple(requirements))


def binding(**overrides):
    values = {
        "project": "UES",
        "route": "INTERNAL:UES",
        "workstream": "W01",
        "role": "WRITER",
        "repo": REPO,
        "branch": "work/ues-auto-v2-a-lifecycle",
        "lifecycle_state": LifecycleState.WRITER_ACTIVE,
        "baseline_sha": BASE,
        "base_ref": "automation/portfolio-control-plane-v2",
        "task_budget_class": "NO_NEW_TASK_REQUIRED",
        "last_activity_at": NOW,
        "writer_lineage": "writer-lineage",
        "reviewer_lineage": "reviewer-lineage",
        "actor_bindings": (actor("WRITER"), actor("REVIEWER")),
        "scope_identity": "domain-a:r2",
        "head_sha": NEW,
        "pr_number": 11,
    }
    values.update(overrides)
    return WorkstreamBinding(**values)


class LifecycleR2Tests(unittest.TestCase):
    def test_external_effect_is_semantic_candidate_not_execution_authority(self):
        result = resolve_next_action(LifecycleContext(LifecycleState.WRITER_ACTIVE))
        self.assertEqual(result.action, NextAction.PUBLISH_CANDIDATE)
        self.assertEqual(result.required_capability, ActionCapability.EXTERNAL_EFFECT)
        self.assertTrue(result.semantic_candidate)
        self.assertFalse(result.executable)

    def test_parent_review_is_control_signal_without_provider_authority(self):
        result = resolve_next_action(
            LifecycleContext(
                LifecycleState.REVIEW_RESULT,
                review_outcome=ReviewOutcome.PASS,
            )
        )
        self.assertEqual(result.action, NextAction.REQUEST_PARENT_REVIEW)
        self.assertEqual(result.required_capability, ActionCapability.CONTROL_SIGNAL)
        self.assertTrue(result.executable)

    def test_ci_and_review_dependent_waiting_are_read_only(self):
        ci_wait = resolve_next_action(
            LifecycleContext(
                LifecycleState.AWAITING_USER_FEEDBACK,
                waiting_class=WaitingClass.CI_DEPENDENT,
            )
        )
        review_wait = resolve_next_action(
            LifecycleContext(
                LifecycleState.AWAITING_USER_FEEDBACK,
                waiting_class=WaitingClass.REVIEW_DEPENDENT,
            )
        )
        self.assertEqual(ci_wait.required_capability, ActionCapability.READ_ONLY)
        self.assertEqual(review_wait.required_capability, ActionCapability.READ_ONLY)
        self.assertTrue(ci_wait.executable)
        self.assertTrue(review_wait.executable)

    def test_recoverable_failure_does_not_self_authorize(self):
        result = resolve_next_action(
            LifecycleContext(
                LifecycleState.FAILED,
                failure_class=FailureClass.RECOVERABLE,
                resume_state=LifecycleState.WRITER_ACTIVE,
            )
        )
        self.assertEqual(result.action, NextAction.RECOVER_SAME_LINEAGE)
        self.assertEqual(result.required_capability, ActionCapability.EXTERNAL_EFFECT)
        self.assertFalse(result.executable)


class ReconciliationR2Tests(unittest.TestCase):
    def test_one_lane_has_proven_writer_and_reviewer_bindings(self):
        item = binding()
        writer = resolve_actor_binding(item, "WRITER")
        reviewer = resolve_actor_binding(item, "REVIEWER")
        self.assertTrue(writer.proven)
        self.assertTrue(reviewer.proven)
        self.assertNotEqual(writer.binding.session_id, reviewer.binding.session_id)

    def test_unique_heuristic_actor_is_not_promoted(self):
        item = binding(
            actor_bindings=(
                actor(
                    "WRITER",
                    status=SourceBindingStatus.PROPOSED_UNVERIFIED,
                ),
            )
        )
        resolved = resolve_actor_binding(item, "WRITER")
        self.assertFalse(resolved.proven)
        self.assertEqual(
            resolved.state,
            SourceBindingStatus.PROPOSED_UNVERIFIED.value,
        )

    def test_missing_writer_blocks_writer_effect_but_not_parent_signal(self):
        only_reviewer = (actor("REVIEWER"),)
        findings = binding(
            role="REVIEWER",
            actor_bindings=only_reviewer,
            lifecycle_state=LifecycleState.REVIEW_RESULT,
            review=review(outcome=ReviewOutcome.FINDINGS),
        )
        blocked = reconcile_workstream(findings)
        self.assertEqual(blocked.resolution.stop_gate, StopGate.ACTOR_BINDING_REQUIRED)
        self.assertIn("unproven:actor_binding:WRITER", blocked.issues)

        passed = binding(
            role="REVIEWER",
            actor_bindings=(),
            lifecycle_state=LifecycleState.REVIEW_RESULT,
            review=review(outcome=ReviewOutcome.PASS),
        )
        parent = reconcile_workstream(passed)
        self.assertEqual(parent.resolution.action, NextAction.REQUEST_PARENT_REVIEW)
        self.assertEqual(
            parent.resolution.required_capability,
            ActionCapability.CONTROL_SIGNAL,
        )
        self.assertEqual(parent.issues, ())
        self.assertTrue(parent.executable)

    def test_missing_reviewer_blocks_review_dispatch(self):
        item = binding(
            actor_bindings=(actor("WRITER"),),
            lifecycle_state=LifecycleState.CI_CLASSIFIED,
            ci=ci(),
        )
        result = reconcile_workstream(item)
        self.assertEqual(result.resolution.stop_gate, StopGate.ACTOR_BINDING_REQUIRED)
        self.assertIn("unproven:actor_binding:REVIEWER", result.issues)

    def test_same_session_across_writer_and_reviewer_role_fails_closed(self):
        shared = "shared-session"
        item = binding(
            actor_bindings=(
                actor("WRITER", session_id=shared),
                actor("REVIEWER", session_id=shared),
            )
        )
        result = reconcile_portfolio([item])[0]
        self.assertEqual(
            result.resolution.stop_gate,
            StopGate.AMBIGUOUS_PROVIDER_SESSION,
        )

    def test_same_session_conflict_does_not_block_unrelated_lane(self):
        shared = "shared-session"
        first = binding(
            project="GS",
            route="PERSONAL:GS",
            workstream="W01",
            actor_bindings=(actor("WRITER", session_id=shared),),
        )
        second = binding(
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W02",
            actor_bindings=(actor("WRITER", session_id=shared),),
        )
        third = binding(
            project="FCP",
            route="INTERNAL:FCP",
            workstream="W03",
            actor_bindings=(actor("WRITER", session_id="independent"),),
        )
        results = reconcile_portfolio([first, second, third])
        self.assertEqual(
            results[0].resolution.stop_gate,
            StopGate.AMBIGUOUS_PROVIDER_SESSION,
        )
        self.assertEqual(
            results[1].resolution.stop_gate,
            StopGate.AMBIGUOUS_PROVIDER_SESSION,
        )
        self.assertNotEqual(
            results[2].resolution.stop_gate,
            StopGate.AMBIGUOUS_PROVIDER_SESSION,
        )

    def test_gs_and_cep_w01_are_distinct_canonical_lanes(self):
        gs = binding(project="GS", route="PERSONAL:GS", workstream="W01")
        cep = binding(
            project="CEP",
            route="PERSONAL:CEP",
            workstream="W01",
            actor_bindings=(
                actor("WRITER", session_id="cep-writer"),
                actor("REVIEWER", session_id="cep-reviewer"),
            ),
        )
        self.assertEqual(canonical_lane_key(gs), ("GS", "PERSONAL:GS", "W01"))
        self.assertEqual(canonical_lane_key(cep), ("CEP", "PERSONAL:CEP", "W01"))
        self.assertNotEqual(canonical_lane_key(gs), canonical_lane_key(cep))

    def test_base_scope_and_profile_drift_fail_closed(self):
        previous = binding(
            evidence_profile=profile(
                EvidenceRequirement("ci", True),
                profile_id="profile-1",
            )
        )
        cases = (
            binding(base_ref="different-base", evidence_profile=previous.evidence_profile),
            binding(scope_identity="different-scope", evidence_profile=previous.evidence_profile),
            binding(
                evidence_profile=profile(
                    EvidenceRequirement("ci", True),
                    profile_id="profile-2",
                )
            ),
        )
        for current in cases:
            with self.subTest(current=current):
                result = reconcile_workstream(current, previous)
                self.assertEqual(
                    result.resolution.stop_gate,
                    StopGate.BINDING_DRIFT_RECONCILIATION_REQUIRED,
                )
                self.assertTrue(any(item.startswith("drift:") for item in result.issues))

    def test_sha_movement_invalidates_prior_exact_sha_evidence(self):
        previous = binding(
            head_sha=OLD,
            lifecycle_state=LifecycleState.REVIEW_RESULT,
            review=review(OLD),
            ci=ci(OLD),
        )
        current = binding(
            head_sha=NEW,
            lifecycle_state=LifecycleState.REVIEW_RESULT,
            review=review(OLD),
            ci=ci(OLD),
        )
        result = reconcile_workstream(current, previous)
        self.assertTrue(result.candidate_sha_moved)
        self.assertTrue(result.prior_review_invalidated)
        self.assertTrue(result.prior_ci_invalidated)
        self.assertEqual(result.binding.lifecycle_state, LifecycleState.NEW_SHA)
        self.assertTrue(result.binding.review.stale)
        self.assertTrue(result.binding.ci.stale)

    def test_generic_missing_browser_profile_evidence_blocks_transition(self):
        item = binding(
            actor_bindings=(),
            lifecycle_state=LifecycleState.REVIEW_RESULT,
            review=review(outcome=ReviewOutcome.PASS),
            evidence_profile=profile(
                EvidenceRequirement(
                    "browser-route-profile",
                    proven=False,
                    actions=(NextAction.REQUEST_PARENT_REVIEW.value,),
                )
            ),
        )
        result = reconcile_workstream(item)
        self.assertEqual(result.resolution.stop_gate, StopGate.EVIDENCE_INCOMPLETE)
        self.assertIn(
            "missing_required_evidence:browser-route-profile",
            result.issues,
        )

    def test_proven_generic_profile_allows_parent_control_signal(self):
        item = binding(
            actor_bindings=(),
            lifecycle_state=LifecycleState.REVIEW_RESULT,
            review=review(outcome=ReviewOutcome.PASS),
            evidence_profile=profile(
                EvidenceRequirement(
                    "browser-route-profile",
                    proven=True,
                    current=True,
                    evidence_id="browser-evidence-1",
                    actions=(NextAction.REQUEST_PARENT_REVIEW.value,),
                )
            ),
        )
        result = reconcile_workstream(item)
        self.assertEqual(result.issues, ())
        self.assertEqual(result.resolution.action, NextAction.REQUEST_PARENT_REVIEW)
        self.assertTrue(result.executable)


if __name__ == "__main__":
    unittest.main()
