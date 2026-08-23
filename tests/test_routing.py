import unittest

from ues.routing import (
    route_reviewer_to_writer,
    route_terminal_session_failure,
    route_waiting,
    route_writer_to_reviewer,
    waiting_routing_table,
)


class RoutingPolicyTests(unittest.TestCase):
    def test_waiting_table_is_complete_and_fail_closed(self):
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

    def test_environment_mismatch_continues_same_session_only_when_authorized(self):
        result = route_waiting(
            "ENVIRONMENT_MISMATCH",
            exact_state_read=True,
            latest_activity_read=True,
            bounded_workaround_authorized=True,
        )
        self.assertEqual(result["authority"], "AUTO_SAFE")
        self.assertEqual(result["action"], "CONTINUE_SAME_SESSION")
        self.assertFalse(result["new_task_justified_by_waiting"])

    def test_waiting_without_exact_reads_fails_closed(self):
        result = route_waiting(
            "POLICY_RESOLVABLE",
            exact_state_read=False,
            latest_activity_read=True,
            project_policy_permits=True,
        )
        self.assertEqual(result["authority"], "DENY")

    def test_reviewer_findings_group_into_one_writer_packet(self):
        result = route_reviewer_to_writer(
            workstream_id="W04",
            reviewed_sha="a" * 40,
            candidate_sha="a" * 40,
            reviewer_role_valid=True,
            reviewer_independent=True,
            reviewer_mutation_detected=False,
            reviewer_mutation_adjudicated=True,
            reviewer_mutation_disqualifying=False,
            writer_binding_proven=True,
            finding_within_writer_scope=True,
            correction_in_flight=False,
            correction_already_sent=False,
            findings=[
                {"id": "F2", "root_cause": "BOUNDARY", "summary": "two", "paths": ["b.py"]},
                {"id": "F1", "root_cause": "BOUNDARY", "summary": "one", "paths": ["a.py"]},
                {"id": "F3", "root_cause": "TEST", "summary": "three", "paths": ["t.py"]},
            ],
        )
        self.assertEqual(result["authority"], "AUTO_SAFE")
        self.assertEqual(result["grouped_packet_count"], 1)
        self.assertEqual(len(result["correction_packet"]), 2)

    def test_reviewer_mutation_and_ambiguous_writer_fail_closed(self):
        result = route_reviewer_to_writer(
            workstream_id="W04",
            reviewed_sha="b" * 40,
            candidate_sha="b" * 40,
            reviewer_role_valid=True,
            reviewer_independent=True,
            reviewer_mutation_detected=True,
            reviewer_mutation_adjudicated=False,
            reviewer_mutation_disqualifying=False,
            writer_binding_proven=False,
            finding_within_writer_scope=True,
            correction_in_flight=False,
            correction_already_sent=False,
            findings=[{"id": "F1", "root_cause": "CODE", "summary": "x"}],
        )
        self.assertEqual(result["authority"], "DENY")
        self.assertIn("REVIEWER_MUTATION_UNADJUDICATED", result["failures"])
        self.assertIn("WRITER_BINDING_UNPROVEN", result["failures"])

    def test_duplicate_correction_is_prevented(self):
        result = route_reviewer_to_writer(
            workstream_id="W04",
            reviewed_sha="c" * 40,
            candidate_sha="c" * 40,
            reviewer_role_valid=True,
            reviewer_independent=True,
            reviewer_mutation_detected=False,
            reviewer_mutation_adjudicated=True,
            reviewer_mutation_disqualifying=False,
            writer_binding_proven=True,
            finding_within_writer_scope=True,
            correction_in_flight=True,
            correction_already_sent=False,
            findings=[{"id": "F1", "root_cause": "CODE", "summary": "x"}],
        )
        self.assertEqual(result["authority"], "DENY")
        self.assertIn("DUPLICATE_OR_IN_FLIGHT_CORRECTION", result["failures"])

    def test_new_sha_invalidates_review_and_reuses_safe_reviewer(self):
        new_sha = "d" * 40
        result = route_writer_to_reviewer(
            prior_reviewed_sha="e" * 40,
            new_candidate_sha=new_sha,
            ci_evidence_sha=new_sha,
            review_evidence_sha=new_sha,
            existing_reviewer_available=True,
            existing_reviewer_safe_to_reuse=True,
            new_reviewer_policy_allows=False,
            parent_gate_satisfied=False,
        )
        self.assertTrue(result["prior_review_stale"])
        self.assertEqual(result["authority"], "AUTO_SAFE")
        self.assertTrue(result["reuse_existing_reviewer"])

    def test_new_reviewer_never_auto_created(self):
        new_sha = "f" * 40
        result = route_writer_to_reviewer(
            prior_reviewed_sha=None,
            new_candidate_sha=new_sha,
            ci_evidence_sha=new_sha,
            review_evidence_sha=new_sha,
            existing_reviewer_available=False,
            existing_reviewer_safe_to_reuse=False,
            new_reviewer_policy_allows=True,
            parent_gate_satisfied=True,
        )
        self.assertEqual(result["authority"], "PARENT_REQUIRED")
        self.assertFalse(result["automatic_new_reviewer_creation"])

    def test_terminal_failed_session_recommends_but_does_not_create_task(self):
        result = route_terminal_session_failure(same_session_available=False)
        self.assertEqual(result["classification"], "SESSION_CONTINUATION_UNAVAILABLE")
        self.assertEqual(result["action"], "NEW_TASK_RECOMMENDED")
        self.assertFalse(result["automatic_new_task_creation"])


if __name__ == "__main__":
    unittest.main()
