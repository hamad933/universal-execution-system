import unittest

from ues.routing import (
    FAILURE_SAME_SESSION_RECOVERY,
    RE_REVIEW_DISPATCH,
    REVIEW_CORRECTION_PACKET,
    WAITING_SAME_SESSION_CONTINUATION,
    classify_waiting_activity,
    route_reviewer_to_writer,
    route_terminal_session_failure,
    route_waiting,
    route_writer_to_reviewer,
    validate_post_review_evidence,
)


class WaitingClassifierR2Tests(unittest.TestCase):
    def test_structured_rule_classifies_without_keyword_shortcut(self):
        result = classify_waiting_activity(
            {
                "activity_type": "question",
                "question_kind": "policy",
                "text": "database architecture db",
            },
            provider_state="AWAITING_USER_FEEDBACK",
            classifier_rules={
                "rules": [
                    {
                        "waiting_class": "POLICY_RESOLVABLE",
                        "match": {
                            "provider_state": "AWAITING_USER_FEEDBACK",
                            "activity_type": "question",
                            "question_kind": "policy",
                        },
                        "evidence": "rule-policy-question-v1",
                    }
                ]
            },
        )
        self.assertEqual(result["waiting_class"], "POLICY_RESOLVABLE")
        self.assertEqual(result["confidence"], "HIGH")
        self.assertFalse(result["keyword_shortcut_used"])
        self.assertEqual(result["authority"], "POLICY_REQUIRED")

    def test_database_keywords_alone_do_not_classify_or_grant_authority(self):
        result = classify_waiting_activity(
            {"text": "database db architecture shared contract"},
            provider_state="AWAITING_USER_FEEDBACK",
            classifier_rules={"rules": []},
        )
        self.assertEqual(result["waiting_class"], "UNCLASSIFIED")
        self.assertEqual(result["authority"], "POLICY_REQUIRED")
        self.assertFalse(result["keyword_shortcut_used"])

    def test_ambiguous_multiple_structured_rules_fails_closed(self):
        rules = {
            "rules": [
                {"waiting_class": "POLICY_RESOLVABLE", "match": {"kind": "question"}},
                {"waiting_class": "ENVIRONMENT_MISMATCH", "match": {"kind": "question"}},
            ]
        }
        result = classify_waiting_activity(
            {"kind": "question"},
            provider_state="AWAITING_USER_FEEDBACK",
            classifier_rules=rules,
        )
        self.assertEqual(result["waiting_class"], "UNCLASSIFIED")
        self.assertEqual(result["confidence"], "LOW")


class RoutingPolicyR2Tests(unittest.TestCase):
    def test_waiting_environment_requires_project_action_authorization(self):
        denied = route_waiting(
            "ENVIRONMENT_MISMATCH",
            exact_state_read=True,
            latest_activity_read=True,
            continuation_binding_proven=True,
            project_auto_safe_actions={REVIEW_CORRECTION_PACKET},
            bounded_workaround_authorized=True,
        )
        self.assertEqual(denied["authority"], "PARENT_REQUIRED")
        self.assertEqual(denied["action"], "ESCALATE_PARENT")
        self.assertEqual(denied["semantic_effect"], WAITING_SAME_SESSION_CONTINUATION)

        allowed = route_waiting(
            "ENVIRONMENT_MISMATCH",
            exact_state_read=True,
            latest_activity_read=True,
            continuation_binding_proven=True,
            project_auto_safe_actions={WAITING_SAME_SESSION_CONTINUATION},
            bounded_workaround_authorized=True,
        )
        self.assertEqual(allowed["authority"], "AUTO_SAFE")
        self.assertEqual(allowed["action"], "CONTINUE_SAME_SESSION")

    def test_waiting_missing_action_policy_fails_closed(self):
        result = route_waiting(
            "POLICY_RESOLVABLE",
            exact_state_read=True,
            latest_activity_read=True,
            continuation_binding_proven=True,
            project_auto_safe_actions=None,
            project_policy_permits=True,
        )
        self.assertEqual(result["authority"], "DENY")
        self.assertIn("PROJECT_AUTO_SAFE_ACTIONS_REQUIRED", result["reasons"])

    def test_ci_and_review_dependent_waiting_are_read_only(self):
        for waiting_class, action in (
            ("CI_DEPENDENT", "RECONCILE_CI_EVIDENCE"),
            ("REVIEW_DEPENDENT", "RECONCILE_REVIEW_EVIDENCE"),
        ):
            with self.subTest(waiting_class=waiting_class):
                result = route_waiting(
                    waiting_class,
                    exact_state_read=True,
                    latest_activity_read=True,
                    continuation_binding_proven=False,
                    project_auto_safe_actions=None,
                    deterministic_evidence=True,
                )
                self.assertEqual(result["authority"], "READ_ONLY")
                self.assertEqual(result["action"], action)
                self.assertIsNone(result["semantic_effect"])

    def test_same_session_unavailable_is_parent_recommendation_not_new_task_creation(self):
        result = route_waiting(
            "POLICY_RESOLVABLE",
            exact_state_read=True,
            latest_activity_read=True,
            continuation_binding_proven=True,
            project_auto_safe_actions={WAITING_SAME_SESSION_CONTINUATION},
            project_policy_permits=True,
            same_session_available=False,
        )
        self.assertEqual(result["authority"], "PARENT_REQUIRED")
        self.assertFalse(result["automatic_new_task_creation"])
        self.assertFalse(result["new_task_justified_by_waiting"])

    def _correction(self, actions):
        return route_reviewer_to_writer(
            project="GS",
            route="PERSONAL:GS",
            workstream_id="W04",
            writer_session_id="writer-123",
            reviewer_session_id="reviewer-456",
            reviewed_sha="a" * 40,
            candidate_sha="a" * 40,
            reviewer_role_valid=True,
            reviewer_independent=True,
            reviewer_mutation_detected=False,
            reviewer_mutation_adjudicated=True,
            reviewer_mutation_disqualifying=False,
            writer_binding_proven=True,
            writer_binding_kind="EXPLICIT",
            finding_within_writer_scope=True,
            canonical_operation_active=False,
            canonical_operation_confirmed=False,
            findings=[
                {"id": "F1", "root_cause": "BOUNDARY", "summary": "one", "paths": ["a.py"]},
                {"id": "F2", "root_cause": "BOUNDARY", "summary": "two", "paths": ["b.py"]},
            ],
            project_auto_safe_actions=actions,
        )

    def test_correction_requires_project_action_policy(self):
        denied = self._correction(set())
        self.assertEqual(denied["authority"], "PARENT_REQUIRED")
        self.assertEqual(denied["action"], "ESCALATE_PARENT_REVIEW_CORRECTION_PACKET")
        self.assertEqual(denied["grouped_packet_count"], 1)
        self.assertFalse(denied["reuse_existing_writer"])

        allowed = self._correction({REVIEW_CORRECTION_PACKET})
        self.assertEqual(allowed["authority"], "AUTO_SAFE")
        self.assertEqual(allowed["action"], "SEND_ONE_CORRECTION_PACKET_TO_EXISTING_WRITER")
        self.assertTrue(allowed["reuse_existing_writer"])
        identity = allowed["operation_identity_input"]
        self.assertEqual(identity["route"], "PERSONAL:GS")
        self.assertEqual(identity["action"], REVIEW_CORRECTION_PACKET)

    def test_unique_heuristic_writer_remains_unproven_even_with_policy(self):
        result = route_reviewer_to_writer(
            project="GS",
            route="PERSONAL:GS",
            workstream_id="W04",
            writer_session_id="only-candidate",
            reviewer_session_id="reviewer",
            reviewed_sha="b" * 40,
            candidate_sha="b" * 40,
            reviewer_role_valid=True,
            reviewer_independent=True,
            reviewer_mutation_detected=False,
            reviewer_mutation_adjudicated=True,
            reviewer_mutation_disqualifying=False,
            writer_binding_proven=True,
            writer_binding_kind="UNIQUE_HEURISTIC",
            finding_within_writer_scope=True,
            canonical_operation_active=False,
            canonical_operation_confirmed=False,
            findings=[{"id": "F1", "root_cause": "CODE", "summary": "x"}],
            project_auto_safe_actions={REVIEW_CORRECTION_PACKET},
        )
        self.assertEqual(result["authority"], "DENY")
        self.assertIn("WRITER_BINDING_UNPROVEN", result["failures"])

    def _rereview(self, actions):
        new_sha = "e" * 40
        return route_writer_to_reviewer(
            project="GS",
            route="PERSONAL:GS",
            workstream_id="W04",
            writer_session_id="writer",
            reviewer_session_id="reviewer",
            prior_reviewed_sha="f" * 40,
            new_candidate_sha=new_sha,
            ci_evidence_sha=new_sha,
            required_ci_proven=True,
            existing_reviewer_available=True,
            existing_reviewer_binding_proven=True,
            existing_reviewer_safe_to_reuse=True,
            new_reviewer_policy_allows=False,
            parent_gate_satisfied=False,
            project_auto_safe_actions=actions,
        )

    def test_rereview_dispatch_requires_project_action_policy(self):
        denied = self._rereview(set())
        self.assertTrue(denied["exact_required_ci_for_new_sha"])
        self.assertFalse(denied["pre_dispatch_review_evidence_required"])
        self.assertEqual(denied["authority"], "PARENT_REQUIRED")
        self.assertEqual(denied["action"], "ESCALATE_PARENT_RE_REVIEW_DISPATCH")

        allowed = self._rereview({RE_REVIEW_DISPATCH})
        self.assertEqual(allowed["authority"], "AUTO_SAFE")
        self.assertEqual(allowed["action"], "DISPATCH_RE_REVIEW_TO_EXISTING_REVIEWER")

    def test_failure_recovery_requires_project_action_policy(self):
        denied = route_terminal_session_failure(
            same_session_available=True,
            project_auto_safe_actions=set(),
        )
        self.assertEqual(denied["authority"], "PARENT_REQUIRED")
        self.assertEqual(denied["action"], "ESCALATE_PARENT")

        allowed = route_terminal_session_failure(
            same_session_available=True,
            project_auto_safe_actions={FAILURE_SAME_SESSION_RECOVERY},
        )
        self.assertEqual(allowed["authority"], "AUTO_SAFE")
        self.assertEqual(allowed["action"], "CONTINUE_SAME_SESSION")

    def test_terminal_unavailable_session_never_auto_creates_task(self):
        result = route_terminal_session_failure(
            same_session_available=False,
            project_auto_safe_actions={FAILURE_SAME_SESSION_RECOVERY},
        )
        self.assertEqual(result["authority"], "PARENT_REQUIRED")
        self.assertEqual(result["action"], "NEW_TASK_RECOMMENDED")
        self.assertFalse(result["automatic_new_task_creation"])

    def test_post_review_exact_sha_validation_is_separate(self):
        good = validate_post_review_evidence(
            current_candidate_sha="3" * 40,
            reviewed_sha="3" * 40,
            reviewer_binding_proven=True,
        )
        stale = validate_post_review_evidence(
            current_candidate_sha="3" * 40,
            reviewed_sha="4" * 40,
            reviewer_binding_proven=True,
        )
        self.assertTrue(good["valid"])
        self.assertFalse(stale["valid"])
        self.assertIn("POST_REVIEW_SHA_MISMATCH", stale["failures"])


if __name__ == "__main__":
    unittest.main()
