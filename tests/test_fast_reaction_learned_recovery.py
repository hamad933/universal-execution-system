import unittest
from pathlib import Path
from ues.recovery_catalog import plan_recovery


class RecoveryCatalogTests(unittest.TestCase):
    def test_waiting_routes_same_session(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "AWAITING_USER_FEEDBACK", "role": "WRITER",
            "same_session_prompt_ready": True, "waiting_has_newer_or_equal_user_response": False,
        })
        self.assertEqual(result["action"], "CONTINUE_SAME_SESSION")
        self.assertTrue(result["external_effect"])

    def test_completed_writer_with_green_ci_routes_to_reviewer(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "WRITER",
            "candidate_sha": "a"*40, "ci_verdict": "PASS", "work_remaining": True,
        })
        self.assertEqual(result["action"], "ROUTE_CURRENT_SHA_TO_REVIEWER_LINEAGE")

    def test_stale_writer_handoff_is_reconciled_to_authoritative_current_sha(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "WRITER",
            "current_sha": "b"*40, "candidate_sha": "b"*40, "ci_verdict": "PASS", "work_remaining": True,
            "handoff": {"status": "COMPLETE", "verdict": "PASS", "candidate_sha": "a"*40},
        })
        self.assertEqual(result["action"], "ROUTE_CURRENT_SHA_TO_REVIEWER_LINEAGE")
        self.assertEqual(result["root_cause"], "STALE_WRITER_HANDOFF_RECONCILED_TO_CURRENT_SHA")
        self.assertFalse(result["external_effect"])

    def test_stale_writer_handoff_never_substitutes_unvalidated_provider_sha(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "WRITER",
            "current_sha": "b"*40, "candidate_sha": "b"*40, "ci_verdict": "FAIL", "work_remaining": True,
            "handoff": {"status": "COMPLETE", "verdict": "PASS", "candidate_sha": "a"*40},
        })
        self.assertEqual(result["action"], "VALIDATE_EXACT_WRITER_CANDIDATE")
        self.assertEqual(result["root_cause"], "STALE_WRITER_HANDOFF_REQUIRES_CURRENT_SHA_VALIDATION")
        self.assertFalse(result["external_effect"])

    def test_failed_session_replacement_preserves_lineage_and_requires_budget(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "FAILED", "role": "WRITER",
            "work_remaining": True, "new_session_budget_safe": False, "replacement_prompt_ready": True,
            "active_duplicate_absent": True,
        })
        self.assertEqual(result["action"], "PREPARE_SAME_LINEAGE_REPLACEMENT")
        self.assertIn("TASK_BUDGET", result["stop_gate"])

    def test_terminal_replacement_requires_explicit_duplicate_free_proof(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "FAILED", "role": "WRITER",
            "work_remaining": True, "new_session_budget_safe": True,
            "replacement_prompt_ready": True,
        })
        self.assertEqual(result["action"], "PREPARE_SAME_LINEAGE_REPLACEMENT")
        self.assertIn("ACTIVE_DUPLICATE_CHECK_REQUIRED", result["stop_gate"])
        self.assertFalse(result["external_effect"])

    def test_unbound_does_not_create_merely_because_budget_and_prompt_exist(self):
        result = plan_recovery({
            "binding_status": "UNBOUND", "provider_state": "UNKNOWN", "role": "WRITER",
            "work_remaining": True, "new_session_budget_safe": True,
            "replacement_prompt_ready": True,
        })
        self.assertEqual(result["action"], "RECONCILE_BINDING_OR_PREPARE_SAME_LINEAGE_REPLACEMENT")
        self.assertFalse(result["external_effect"])

    def test_unbound_can_create_only_after_replacement_and_duplicate_safety_are_proven(self):
        result = plan_recovery({
            "binding_status": "UNBOUND", "provider_state": "UNKNOWN", "role": "WRITER",
            "work_remaining": True, "new_session_budget_safe": True,
            "replacement_prompt_ready": True, "replacement_required_proven": True,
            "active_duplicate_absent": True,
        })
        self.assertEqual(result["action"], "CREATE_OR_ADOPT_SAME_LOGICAL_LINEAGE_GENERATION")
        self.assertTrue(result["external_effect"])

    def test_unknown_write_precedes_terminal_replacement(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "FAILED", "role": "WRITER",
            "work_remaining": True, "new_session_budget_safe": True,
            "replacement_prompt_ready": True, "unknown_write_state": True,
            "active_duplicate_absent": True,
        })
        self.assertEqual(result["action"], "AUTHORITATIVE_POST_WRITE_RECONCILIATION")

    def test_stale_completed_reviewer_can_start_next_generation_for_current_sha(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "REVIEWER",
            "current_sha": "c"*40,
            "handoff": {"reviewed_sha": "d"*40, "verdict": "PASS"},
            "work_remaining": True, "new_session_budget_safe": True,
            "replacement_prompt_ready": True, "active_duplicate_absent": True,
        })
        self.assertEqual(result["action"], "CREATE_NEXT_SESSION_GENERATION_SAME_LINEAGE")
        self.assertEqual(result["root_cause"], "STALE_REVIEW_REQUIRES_CURRENT_SHA_REREVIEW")

    def test_unstructured_completed_reviewer_is_adjudicated_not_replaced_blindly(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "REVIEWER",
            "work_remaining": True, "new_session_budget_safe": True,
            "replacement_prompt_ready": True,
        })
        self.assertEqual(result["action"], "PARENT_ADJUDICATE_UNSTRUCTURED_COMPLETED_REVIEW")
        self.assertFalse(result["external_effect"])

    def test_shared_lineage_safety_net_is_five_minutes(self):
        workflow = Path(".github/workflows/ues-bounded-existing-session.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "*/5 * * * *"', workflow)
        self.assertNotIn('cron: "*/10 * * * *"', workflow)


if __name__ == "__main__":
    unittest.main()
