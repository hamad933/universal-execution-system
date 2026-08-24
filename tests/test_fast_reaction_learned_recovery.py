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

    def test_external_intent_persisted_without_provider_readback_waits_fail_closed(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "WRITER",
            "work_remaining": True, "new_session_budget_safe": True,
            "replacement_prompt_ready": True, "active_duplicate_absent": True,
            "external_intent": {
                "signal_persisted": True,
                "provider_ack_proven": False,
                "provider_session_readback_proven": False,
                "provider_observation_after_signal_complete": False,
            },
        })
        self.assertEqual(result["action"], "WAIT_FOR_AUTHORITATIVE_PROVIDER_OBSERVATION_AFTER_EXTERNAL_INTENT")
        self.assertEqual(result["root_cause"], "EXTERNAL_INTENT_PERSISTED_PROVIDER_EFFECT_UNKNOWN")
        self.assertIn("NO_BLIND_RETRY", result["stop_gate"])
        self.assertFalse(result["external_effect"])

    def test_external_intent_ack_without_session_binding_reconciles_fail_closed(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "FAILED", "role": "WRITER",
            "work_remaining": True, "new_session_budget_safe": True,
            "replacement_prompt_ready": True, "active_duplicate_absent": True,
            "external_intent": {
                "signal_persisted": True,
                "provider_ack_proven": True,
                "provider_session_readback_proven": False,
                "provider_observation_after_signal_complete": True,
            },
        })
        self.assertEqual(result["action"], "RECONCILE_EXTERNAL_INTENT_ACK_TO_PROVIDER_SESSION_BINDING")
        self.assertEqual(result["root_cause"], "EXTERNAL_INTENT_ACK_WITHOUT_AUTHORITATIVE_SESSION_BINDING")
        self.assertIn("NO_BLIND_RETRY", result["stop_gate"])
        self.assertFalse(result["external_effect"])

    def test_external_intent_no_effect_after_authoritative_observation_does_not_create_generation(self):
        result = plan_recovery({
            "binding_status": "UNBOUND", "provider_state": "UNKNOWN", "role": "WRITER",
            "work_remaining": True, "replacement_required_proven": True,
            "new_session_budget_safe": True, "replacement_prompt_ready": True,
            "active_duplicate_absent": True,
            "external_intent": {
                "signal_persisted": True,
                "provider_ack_proven": False,
                "provider_session_readback_proven": False,
                "provider_observation_after_signal_complete": True,
            },
        })
        self.assertEqual(result["action"], "RECONCILE_EXTERNAL_INTENT_NO_EFFECT")
        self.assertEqual(
            result["root_cause"],
            "EXTERNAL_INTENT_PERSISTED_NO_PROVIDER_SESSION_AFTER_AUTHORITATIVE_OBSERVATION",
        )
        self.assertIn("NO_BLIND_RETRY", result["stop_gate"])
        self.assertFalse(result["external_effect"])

    def test_unknown_write_precedes_external_intent_reconciliation(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "FAILED", "role": "WRITER",
            "unknown_write_state": True,
            "external_intent": {
                "signal_persisted": True,
                "provider_ack_proven": True,
                "provider_session_readback_proven": False,
                "provider_observation_after_signal_complete": True,
            },
        })
        self.assertEqual(result["action"], "AUTHORITATIVE_POST_WRITE_RECONCILIATION")
        self.assertEqual(result["root_cause"], "UNKNOWN_PROVIDER_WRITE")
        self.assertFalse(result["external_effect"])

    def test_external_intent_with_authoritative_session_readback_allows_normal_lifecycle(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "WRITER",
            "candidate_sha": "a"*40, "ci_verdict": "PASS", "work_remaining": True,
            "external_intent": {
                "signal_persisted": True,
                "provider_ack_proven": True,
                "provider_session_readback_proven": True,
                "provider_observation_after_signal_complete": True,
            },
        })
        self.assertEqual(result["action"], "ROUTE_CURRENT_SHA_TO_REVIEWER_LINEAGE")
        self.assertFalse(result["external_effect"])

    def test_completed_writer_with_green_ci_routes_to_reviewer(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "WRITER",
            "candidate_sha": "a"*40, "ci_verdict": "PASS", "work_remaining": True,
        })
        self.assertEqual(result["action"], "ROUTE_CURRENT_SHA_TO_REVIEWER_LINEAGE")

    def test_repeated_writer_noop_successors_can_start_guarded_same_lineage_replacement(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "WRITER",
            "candidate_sha": "a"*40, "work_remaining": True,
            "consecutive_noop_writer_successors": 2, "noop_evidence_authoritative": True,
            "new_session_budget_safe": True, "replacement_prompt_ready": True,
            "active_duplicate_absent": True,
        })
        self.assertEqual(result["action"], "CREATE_NEXT_SESSION_GENERATION_SAME_LINEAGE")
        self.assertEqual(result["root_cause"], "REPEATED_WRITER_NOOP_SESSION_INEFFECTIVE")
        self.assertTrue(result["external_effect"])

    def test_repeated_writer_noop_successors_still_require_replacement_guards(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "WRITER",
            "candidate_sha": "a"*40, "work_remaining": True,
            "consecutive_noop_writer_successors": 2, "noop_evidence_authoritative": True,
            "new_session_budget_safe": False, "replacement_prompt_ready": True,
        })
        self.assertEqual(result["action"], "PREPARE_SAME_LINEAGE_REPLACEMENT")
        self.assertEqual(result["root_cause"], "REPEATED_WRITER_NOOP_SESSION_INEFFECTIVE")
        self.assertIn("ACTIVE_DUPLICATE_CHECK_REQUIRED", result["stop_gate"])
        self.assertIn("TASK_BUDGET_OR_NEW_SESSION_AUTHORITY", result["stop_gate"])
        self.assertFalse(result["external_effect"])

    def test_repeated_writer_noop_claim_requires_authoritative_delta_evidence(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "WRITER",
            "candidate_sha": "a"*40, "work_remaining": True,
            "consecutive_noop_writer_successors": 2,
            "new_session_budget_safe": True, "replacement_prompt_ready": True,
            "active_duplicate_absent": True,
        })
        self.assertEqual(result["action"], "RECONCILE_AUTHORITATIVE_WRITER_DELTA_EVIDENCE")
        self.assertEqual(result["root_cause"], "REPEATED_WRITER_NOOP_EVIDENCE_UNPROVEN")
        self.assertFalse(result["external_effect"])

    def test_single_writer_noop_does_not_force_replacement(self):
        result = plan_recovery({
            "binding_status": "PROVEN", "provider_state": "COMPLETED", "role": "WRITER",
            "candidate_sha": "a"*40, "ci_verdict": "FAIL", "work_remaining": True,
            "consecutive_noop_writer_successors": 1, "noop_evidence_authoritative": True,
            "new_session_budget_safe": True, "replacement_prompt_ready": True,
            "active_duplicate_absent": True,
        })
        self.assertEqual(result["action"], "VALIDATE_EXACT_WRITER_CANDIDATE")
        self.assertEqual(result["root_cause"], "WRITER_COMPLETED_REQUIRES_EXACT_HEAD_EVIDENCE")
        self.assertFalse(result["external_effect"])

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
