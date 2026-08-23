import unittest

from ues.routing import (
    route_reviewer_to_writer,
    route_terminal_session_failure,
    route_waiting,
    route_writer_to_reviewer,
    validate_post_review_evidence,
    waiting_routing_table,
)


class RoutingPolicyTests(unittest.TestCase):
    def test_waiting_table_is_generic_capability_not_project_authority(self):
        table = waiting_routing_table()
        self.assertEqual(table["POLICY_RESOLVABLE"], "AUTO_SAFE")
        self.assertEqual(table["ENVIRONMENT_MISMATCH"], "AUTO_SAFE")
        self.assertEqual(table["CI_DEPENDENT"], "AUTO_SAFE")
        self.assertEqual(table["REVIEW_DEPENDENT"], "AUTO_SAFE")
        self.assertEqual(table["TOOL_LIMIT"], "AUTO_SAFE")
        self.assertEqual(table["SHARED_CONTRACT_REQUIRED"], "PARENT_REQUIRED")
        self.assertEqual(table["SCOPE_OR_NEW_TASK_REQUIRED"], "PARENT_REQUIRED")
        self.assertEqual(table["OWNER_DECISION_REQUIRED"], "OWNER_REQUIRED")
        self.assertEqual(table["UNCLASSIFIED"], "DENY")

    def test_gs_allowlist_denies_environment_auto_mutation(self):
        result = route_waiting(
            "ENVIRONMENT_MISMATCH",
            exact_state_read=True,
            latest_activity_read=True,
            continuation_binding_proven=True,
            project_auto_safe_allowlist={"POLICY_RESOLVABLE"},
            bounded_workaround_authorized=True,
        )
        self.assertEqual(result["generic_authority"], "AUTO_SAFE")
        self.assertEqual(result["authority"], "PARENT_REQUIRED")
        self.assertEqual(result["action"], "ESCALATE_PARENT")
        self.assertFalse(result["project_auto_safe_authorized"])

    def test_project_explicitly_allows_environment_when_other_predicates_hold(self):
        result = route_waiting(
            "ENVIRONMENT_MISMATCH",
            exact_state_read=True,
            latest_activity_read=True,
            continuation_binding_proven=True,
            project_auto_safe_allowlist={"ENVIRONMENT_MISMATCH"},
            bounded_workaround_authorized=True,
        )
        self.assertEqual(result["authority"], "AUTO_SAFE")
        self.assertEqual(result["action"], "CONTINUE_SAME_SESSION")
        self.assertTrue(result["project_auto_safe_authorized"])

    def test_missing_project_allowlist_fails_closed(self):
        result = route_waiting(
            "POLICY_RESOLVABLE",
            exact_state_read=True,
            latest_activity_read=True,
            continuation_binding_proven=True,
            project_auto_safe_allowlist=None,
            project_policy_permits=True,
        )
        self.assertEqual(result["authority"], "DENY")
        self.assertIn("PROJECT_AUTO_SAFE_ALLOWLIST_REQUIRED", result["reasons"])

    def test_waiting_without_proven_binding_fails_closed(self):
        result = route_waiting(
            "POLICY_RESOLVABLE",
            exact_state_read=True,
            latest_activity_read=True,
            continuation_binding_proven=False,
            project_auto_safe_allowlist={"POLICY_RESOLVABLE"},
            project_policy_permits=True,
        )
        self.assertEqual(result["authority"], "DENY")
        self.assertIn("CONTINUATION_BINDING_UNPROVEN", result["reasons"])

    def test_same_session_unavailable_returns_parent_recommendation_only(self):
        result = route_waiting(
            "POLICY_RESOLVABLE",
            exact_state_read=True,
            latest_activity_read=True,
            continuation_binding_proven=True,
            project_auto_safe_allowlist={"POLICY_RESOLVABLE"},
            project_policy_permits=True,
            same_session_available=False,
        )
        self.assertEqual(result["authority"], "PARENT_REQUIRED")
        self.assertEqual(result["action"], "PARENT_CONTINUATION_OR_NEW_TASK_RECOMMENDATION")
        self.assertFalse(result["automatic_new_task_creation"])
        self.assertFalse(result["new_task_justified_by_waiting"])

    def test_waiting_without_exact_reads_fails_closed(self):
        result = route_waiting(
            "POLICY_RESOLVABLE",
            exact_state_read=False,
            latest_activity_read=True,
            continuation_binding_proven=True,
            project_auto_safe_allowlist={"POLICY_RESOLVABLE"},
            project_policy_permits=True,
        )
        self.assertEqual(result["authority"], "DENY")

    def test_reviewer_findings_group_into_one_writer_packet_and_identity_input(self):
        result = route_reviewer_to_writer(
            project="GS",
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
                {"id": "F2", "root_cause": "BOUNDARY", "summary": "two", "paths": ["b.py"]},
                {"id": "F1", "root_cause": "BOUNDARY", "summary": "one", "paths": ["a.py"]},
                {"id": "F3", "root_cause": "TEST", "summary": "three", "paths": ["t.py"]},
            ],
        )
        self.assertEqual(result["authority"], "AUTO_SAFE")
        self.assertEqual(result["grouped_packet_count"], 1)
        self.assertEqual(len(result["correction_packet"]), 2)
        identity = result["operation_identity_input"]
        self.assertEqual(identity["identity_owner"], "DOMAIN_D_OR_INTEGRATION")
        self.assertEqual(identity["project"], "GS")
        self.assertEqual(identity["action"], "REVIEW_CORRECTION_PACKET")
        self.assertEqual(identity["identity"]["writer_session_id"], "writer-123")
        self.assertEqual(identity["identity"]["reviewer_session_id"], "reviewer-456")
        self.assertEqual(len(identity["identity"]["effect_identity"]), 3)
        self.assertNotIn("correction_operation_key", result)

    def test_unique_heuristic_writer_binding_is_not_proven(self):
        result = route_reviewer_to_writer(
            project="GS",
            workstream_id="W04",
            writer_session_id="only-candidate",
            reviewer_session_id="reviewer-1",
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
        )
        self.assertEqual(result["authority"], "DENY")
        self.assertIn("WRITER_BINDING_UNPROVEN", result["failures"])

    def test_finding_effect_identity_is_required_for_canonical_operation_identity(self):
        result = route_reviewer_to_writer(
            project="GS",
            workstream_id="W04",
            writer_session_id="writer",
            reviewer_session_id="reviewer",
            reviewed_sha="9" * 40,
            candidate_sha="9" * 40,
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
            findings=[{"root_cause": "CODE", "summary": "missing stable identity"}],
        )
        self.assertEqual(result["authority"], "DENY")
        self.assertIn("FINDING_EFFECT_IDENTITY_REQUIRED", result["failures"])

    def test_reviewer_mutation_must_be_adjudicated(self):
        result = route_reviewer_to_writer(
            project="CEP",
            workstream_id="W04",
            writer_session_id="writer",
            reviewer_session_id="reviewer",
            reviewed_sha="c" * 40,
            candidate_sha="c" * 40,
            reviewer_role_valid=True,
            reviewer_independent=True,
            reviewer_mutation_detected=True,
            reviewer_mutation_adjudicated=False,
            reviewer_mutation_disqualifying=False,
            writer_binding_proven=True,
            writer_binding_kind="EXPLICIT",
            finding_within_writer_scope=True,
            canonical_operation_active=False,
            canonical_operation_confirmed=False,
            findings=[{"id": "F1", "root_cause": "CODE", "summary": "x"}],
        )
        self.assertEqual(result["authority"], "DENY")
        self.assertIn("REVIEWER_MUTATION_UNADJUDICATED", result["failures"])

    def test_canonical_active_or_confirmed_operation_prevents_duplicate(self):
        result = route_reviewer_to_writer(
            project="CEP",
            workstream_id="W04",
            writer_session_id="writer",
            reviewer_session_id="reviewer",
            reviewed_sha="d" * 40,
            candidate_sha="d" * 40,
            reviewer_role_valid=True,
            reviewer_independent=True,
            reviewer_mutation_detected=False,
            reviewer_mutation_adjudicated=True,
            reviewer_mutation_disqualifying=False,
            writer_binding_proven=True,
            writer_binding_kind="CANONICAL",
            finding_within_writer_scope=True,
            canonical_operation_active=True,
            canonical_operation_confirmed=False,
            findings=[{"id": "F1", "root_cause": "CODE", "summary": "x"}],
        )
        self.assertEqual(result["authority"], "DENY")
        self.assertIn("DUPLICATE_OR_IN_FLIGHT_CORRECTION", result["failures"])

    def test_new_sha_exact_ci_and_reusable_reviewer_allows_dispatch_without_review_evidence(self):
        new_sha = "e" * 40
        result = route_writer_to_reviewer(
            project="GS",
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
        )
        self.assertTrue(result["prior_review_stale"])
        self.assertTrue(result["exact_required_ci_for_new_sha"])
        self.assertFalse(result["pre_dispatch_review_evidence_required"])
        self.assertEqual(result["authority"], "AUTO_SAFE")
        self.assertEqual(result["action"], "DISPATCH_RE_REVIEW_TO_EXISTING_REVIEWER")
        self.assertEqual(result["operation_identity_input"]["action"], "RE_REVIEW_DISPATCH")

    def test_reviewer_binding_must_be_proven_before_rereview_dispatch(self):
        new_sha = "1" * 40
        result = route_writer_to_reviewer(
            project="GS",
            workstream_id="W04",
            writer_session_id="writer",
            reviewer_session_id="unique-heuristic-reviewer",
            prior_reviewed_sha="2" * 40,
            new_candidate_sha=new_sha,
            ci_evidence_sha=new_sha,
            required_ci_proven=True,
            existing_reviewer_available=True,
            existing_reviewer_binding_proven=False,
            existing_reviewer_safe_to_reuse=True,
            new_reviewer_policy_allows=False,
            parent_gate_satisfied=False,
        )
        self.assertEqual(result["authority"], "DENY")
        self.assertIn("EXISTING_REVIEWER_BINDING_UNPROVEN", result["failures"])

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

    def test_new_reviewer_never_auto_created(self):
        new_sha = "5" * 40
        result = route_writer_to_reviewer(
            project="CEP",
            workstream_id="W09",
            writer_session_id="writer",
            reviewer_session_id=None,
            prior_reviewed_sha=None,
            new_candidate_sha=new_sha,
            ci_evidence_sha=new_sha,
            required_ci_proven=True,
            existing_reviewer_available=False,
            existing_reviewer_binding_proven=False,
            existing_reviewer_safe_to_reuse=False,
            new_reviewer_policy_allows=True,
            parent_gate_satisfied=True,
        )
        self.assertEqual(result["authority"], "PARENT_REQUIRED")
        self.assertEqual(result["action"], "PARENT_MAY_CREATE_NEW_REVIEWER")
        self.assertFalse(result["automatic_new_reviewer_creation"])

    def test_terminal_failed_session_recommends_but_does_not_create_task(self):
        result = route_terminal_session_failure(same_session_available=False)
        self.assertEqual(result["classification"], "SESSION_CONTINUATION_UNAVAILABLE")
        self.assertEqual(result["action"], "NEW_TASK_RECOMMENDED")
        self.assertFalse(result["automatic_new_task_creation"])


if __name__ == "__main__":
    unittest.main()
